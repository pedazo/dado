"""Local isolated execution environments for DADO (phase 7 foundations).

This module implements an explicit, purely local isolated-snapshot workflow that
serves as an acceptable alternative to git worktrees. Nothing here depends on
git being present, and nothing commits, pushes, or calls out to VCS at all.

Workflow
--------
1. ``create_snapshot`` copies an explicitly declared scope (current dirty
   checkout, including untracked user files) into an isolated environment and
   records baseline metadata (per-file content hashes + modes, exclusions,
   scope) plus a TRUSTED copy in the project-local registry (see below).
2. Independent tasks work in their own separated environments; the original
   checkout is never modified by this module.
3. ``integrate_snapshot`` later copies the environment's relevant *changed*
   files back into the project checkout, comparing the trusted baseline against
   the current destination:
   - an overlapping change (the file changed in the environment AND in the
     project since the snapshot, differently) raises :class:`ConflictError`
     with the list and change nothing (all-or-nothing);
   - re-integrating an environment whose changes are already applied to a
     destination that matches the environment byte-for-byte (content and mode)
     is an idempotent no-op: it never overwrites later destination edits —
     a destination that has moved on differently is reported as a conflict;
   - destination changes outside the declared integrate scope are always
     retained (untouched); the integrate scope can only NARROW the recorded
     snapshot scope, never widen it;
   - deletions and new files are honoured (including new-vs-new overlap
     detection between the environment and the destination), binary files
     round-trip byte-exact, and file modes recorded at snapshot time are
     preserved on apply; a mode change either side counts as a change and can
     conflict just like a content change;
   - symlink escapes and destination-path escapes are rejected before any
     write; FIFOs/sockets/devices are never read or copied.

Trust binding (cross-execution / cross-project safety)
------------------------------------------------------
``snapshot.json`` inside a snapshot directory is convenient metadata but is
NOT trusted for authorization: anyone holding the directory could edit it.
The authoritative association and copy of baseline facts is stored in the
project-LOCAL registry (``<project>/.dado/local/isolation/registry.json``),
keyed by execution ID, written under a lock at snapshot time:

    executions[execution_id] = {
        "project": ...        # the checkout that created the snapshot
        "snapshot": ...       # exact snapshot directory
        "scope": [...]        # immutable record of the snapshot scope
        "base_manifest": {...}# immutable per-file {hash, mode} baseline
    }

``integrate_snapshot`` refuses unless the registry of the DESTINATION project
contains that execution ID with a matching snapshot path, and it derives all
conflict decisions from this trusted ``base_manifest``/``scope`` — never from
the editable ``snapshot.json``. A snapshot made in checkout A therefore cannot
be integrated into checkout B, and a widened/tampered ``snapshot.json`` cannot
expand scope or fabricate entries. The registry itself is the local trust
anchor of the checkout (like ``.git`` refs); the same caveat applies.

Resource reservations
---------------------
"Resource reservations" (ports, databases, build dirs, any opaque label such as
``port:4319``, ``db:work.sqlite``, ``build:dist``) are recorded in the SAME
project-local registry. It coordinates executions against this checkout only;
it is explicitly *not* a distributed lock and makes no lock claims beyond this
project's filesystem. Reservation state is released only by an explicit call to
``release`` (never implicitly, never by garbage collection).

Snapshot location
-----------------
By default snapshots live under ``<project>/.dado/local/isolation/`` (an
excluded, operational directory; the whole ``.dado`` tree is excluded from
snapshots by default so snapshots never nest). A caller may instead pass an
explicit ``root`` anywhere else (caller-provided path); even then the trust
binding is recorded in the creating project's registry.

Durability / reconciliation statement (do NOT overclaim)
--------------------------------------------------------
DADO's transactions/journal API (``store.Store``) is NOT ready at the time this
module was written. Therefore integration uses a self-contained *staging +
preflight + local per-file journal* approach:

- the full change plan is resolved and conflict-checked BEFORE anything is
  written (all-or-nothing on conflicts; destination paths preflight-checked
  against escapes);
- files are staged into a local journal directory under the snapshot first,
  then applied one file at a time with atomic ``os.replace`` writes; the
  journal records each applied step;
- if the process is interrupted mid-apply, the destination keeps whatever was
  applied and the environment remains untouched (integration never edits the
  environment); re-running ``integrate_snapshot(..., resume=True)`` continues
  from the journal and finalizes it, and ``discard_journal`` is the explicit
  reconcile action. A journal whose plan is fully accounted for (empty resume
  residual, or every pending entry already recorded as applied) is finalized
  automatically instead of blocking future integrations;
- a crash between the last apply and the journal cleanup is recovered on the
  next run by that same auto-finalization rule.

This journal is local, best-effort, and module-owned; it is NOT presented as
the durable transaction API and must not be advertised as one. When the real
transactions API lands, integration should be migrated to it: this module's
"preflight plan" and "apply step" call boundaries (``_plan_candidates`` /
``_apply_one``) are the intended seam.

CLI
---
``main(argv)`` is CLI-compatible for later ``dado isolate`` wiring:

  dado isolate create --project P --scope src [--scope more] [--to DIR]
                      [--id ID] [--exclude PAT ...] [--reserve LABEL ...]
  dado isolate list   --project P [--dir DIR]
  dado isolate integrate --project P --snapshot ID_OR_PATH
                         [--scope path ...] [--resume]

Each command prints one JSON result object and returns 0; expected errors print
JSON to stderr and return 1.  The top-level CLI can dispatch by calling
``isolation.main(argv[1:])`` directly; no CLI-module edits were made here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import ConflictError, DadoError, ValidationError
from .fs import atomic_write, file_lock

__all__ = [
    "IntegrationInterrupted",
    "ISOLATION_VERSION",
    "DEFAULT_EXCLUSIONS",
    "SECRET_PATTERNS",
    "ALWAYS_EXCLUDED",
    "default_isolation_root",
    "new_execution_id",
    "create_snapshot",
    "integrate_snapshot",
    "list_snapshots",
    "load_metadata",
    "load_binding",
    "reserve",
    "release",
    "resume_journal",
    "discard_journal",
    "main",
]

ISOLATION_VERSION = 2

#: Directory names / patterns never included in snapshots (per path component).
#: Operational state, caches, virtualenvs; secrets additionally refused even if
#: explicitly requested in a scope (see SECRET_PATTERNS).
ALWAYS_EXCLUDED: tuple[str, ...] = (
    ".git",
    ".dado",  # DADO operational state, including older snapshots of itself
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    ".idea",
    ".vscode",
)
#: Secret-looking paths: excluded by default AND rejected when explicitly named.
SECRET_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.keystore",
    "credentials*",
    "secrets*",
)
DEFAULT_EXCLUSIONS: tuple[str, ...] = ALWAYS_EXCLUDED + SECRET_PATTERNS


class IntegrationInterrupted(DadoError):
    """Raised when an interrupted integration left a resumable local journal."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_mode(path: Path) -> int:
    """Permission bits of a file (follows symlinks, as stat-based compares do)."""
    return path.stat().st_mode & 0o7777


def _as_posix(rel: Path) -> str:
    return rel.as_posix()


def _match(name: str, patterns: Iterable[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _excluded(rel: str, exclusions: Iterable[str]) -> bool:
    """True when any path component of ``rel`` matches an exclusion pattern.

    Patterns are matched against individual components and the full relative
    path, so ``.venv`` excludes everything beneath it and ``*.pyc`` excludes
    compiled files anywhere.
    """
    parts = PurePosixPath(rel).parts
    for pattern in exclusions:
        if _match(parts[-1], [pattern]) or _match(rel, [pattern]):
            return True
        if any(_match(part, [pattern]) for part in parts[:-1]):
            return True
    return False


def default_isolation_root(project: Path) -> Path:
    return project / ".dado" / "local" / "isolation"


def new_execution_id() -> str:
    return f"x-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Project-local execution registry (trust binding + reservations; explicitly
# NOT a distributed lock -- it only coordinates executions against this
# checkout).
# --------------------------------------------------------------------------


def _registry_path(project: Path) -> Path:
    return default_isolation_root(project) / "registry.json"


def _load_registry(project: Path) -> dict[str, Any]:
    path = _registry_path(project)
    if not path.exists():
        return {"version": 1, "executions": {}}
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("executions"), dict):
        raise ValidationError(f"Corrupt local isolation registry: {path}")
    return data


def _save_registry(project: Path, registry: dict[str, Any]) -> None:
    path = _registry_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(registry, indent=2, sort_keys=True) + "\n")


def reserve(project: Path, execution_id: str, labels: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Reserve named resources (opaque labels) for ``execution_id``.

    Labels are advisory, local-only reservations shared by executions on the
    SAME checkout. Acquiring a label already held by another execution fails
    with :class:`ConflictError`. Released only by an explicit ``release`` call.
    """
    if not labels:
        raise ValidationError("reserve() requires at least one resource label")
    with file_lock(default_isolation_root(project) / "registry"):
        registry = _load_registry(project)
        for exec_id, record in registry["executions"].items():
            if exec_id == execution_id:
                continue
            clash = sorted(set(record.get("reservations", [])) & set(labels))
            if clash:
                raise ConflictError(
                    f"Resource labels {clash} already reserved by execution '{exec_id}' "
                    "(local registry only; not a distributed lock)"
                )
        record = registry["executions"].setdefault(
            execution_id, {"created_at": _now(), "reservations": [], "snapshot": None}
        )
        record["reservations"] = sorted(set(record.get("reservations", [])) | set(labels))
        _save_registry(project, registry)
    return {"execution": execution_id, "reservations": record["reservations"]}


def release(project: Path, execution_id: str, labels: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Explicitly release reserved labels (all of them when ``labels`` is None).

    Release is only ever explicit; nothing in this module releases implicitly.
    Unknown execution/labels are a no-op reported in the result.
    """
    with file_lock(default_isolation_root(project) / "registry"):
        registry = _load_registry(project)
        record = registry["executions"].get(execution_id)
        released: list[str] = []
        if record is not None:
            if labels is None:
                released = sorted(record.get("reservations", []))
                record["reservations"] = []
            else:
                held = set(record.get("reservations", []))
                released = sorted(held & set(labels))
                record["reservations"] = sorted(held - set(labels))
        _save_registry(project, registry)
    return {"execution": execution_id, "released": released}


# --------------------------------------------------------------------------
# Snapshot creation
# --------------------------------------------------------------------------


def _validate_scope(project: Path, scope: list[str], exclusions: list[str]) -> list[tuple[Path, str]]:
    """Resolve and validate scope roots. Returns [(abs_path, project_rel)].

    Scope paths are always validated against ALWAYS_EXCLUDED + SECRET_PATTERNS
    (plus caller-provided extra patterns): an explicit scope cannot smuggle a
    cache file, DADO state, or secret material into the snapshot.
    """
    if not scope:
        raise ValidationError("Snapshot requires an explicit scope (list of paths)")
    guarded = list(dict.fromkeys(list(ALWAYS_EXCLUDED) + list(SECRET_PATTERNS) + list(exclusions)))
    validated: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for item in scope:
        raw = Path(item).expanduser()
        abs_path = raw if raw.is_absolute() else project / raw
        abs_path = abs_path.resolve()
        try:
            rel = abs_path.relative_to(project)
        except ValueError:
            raise ValidationError(
                f"Scope path escapes the project: {item} -> {abs_path}"
            ) from None
        rel_str = rel.as_posix()
        if rel_str.startswith(".dado/") or rel_str == ".dado":
            raise ValidationError("Snapshot scope may not include '.dado' operational state")
        if not abs_path.exists():
            raise ValidationError(f"Scope path does not exist: {item}")
        if _excluded(rel_str, guarded):
            raise ValidationError(
                f"Scope path matches excluded patterns (secrets/caches/operational state): {item}"
            )
        if rel_str in seen:
            continue
        seen.add(rel_str)
        validated.append((abs_path, rel_str))
    return validated


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def create_snapshot(
    project: Path,
    scope: list[str],
    *,
    root: Path | None = None,
    execution_id: str | None = None,
    exclusions: list[str] | None = None,
    reservations: list[str] | None = None,
) -> dict[str, Any]:
    """Snapshot the declared scope of the current (possibly dirty) checkout.

    Copies every regular file under the scope paths -- dirty, untracked, git or
    no git -- into ``<root>/<execution_id>/files/`` and writes baseline metadata
    (``snapshot.json``) with per-file sha256 hashes and modes. The default root
    is ``<project>/.dado/local/isolation`` (excluded operational state); pass
    ``root`` to accept a caller-provided location. No git, no commits, no push.

    Trusting: an immutable copy of the scope and the full baseline manifest
    (hash + mode per file) is recorded in the CREATING project's local registry,
    keyed by execution ID; ``integrate_snapshot`` uses only that trusted record
    (see the module docstring "Trust binding").

    Returns the metadata dictionary (including the concrete ``root``).
    """
    project = project.resolve()
    execution_id = execution_id or new_execution_id()
    exclusion_patterns = list(exclusions) if exclusions is not None else list(DEFAULT_EXCLUSIONS)
    scope_resolved = _validate_scope(project, scope, exclusion_patterns)
    isolation_root = (root or default_isolation_root(project)).resolve()
    extraction_dir = isolation_root / execution_id
    if extraction_dir.exists():
        raise ConflictError(f"Snapshot already exists: {extraction_dir}")
    if reservations:
        reserve(project, execution_id, reservations)

    files_dir = extraction_dir / "files"
    files: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    escaped: list[str] = []
    for abs_root, rel_root in scope_resolved:
        if abs_root.is_file():
            _snapshot_file(project, abs_root, rel_root, files_dir, files, skipped, escaped)
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root, followlinks=False):
            current = Path(dirpath)
            # Directory symlinks: do not follow; record; error on escape.
            for dname in list(dirnames):
                dpath = current / dname
                if dpath.is_symlink():
                    rel = _as_posix(dpath.relative_to(project))
                    dirnames.remove(dname)
                    if _inside(project, dpath):
                        skipped[rel] = "symlinked directory (not followed)"
                    else:
                        escaped.append(rel)
                    continue
                rel_dpath = _as_posix(dpath.relative_to(project))
                if _excluded(rel_dpath, exclusion_patterns):
                    dirnames.remove(dname)
            for fname in filenames:
                fpath = current / fname
                rel_fpath = _as_posix(fpath.relative_to(project))
                if _excluded(rel_fpath, exclusion_patterns):
                    skipped[rel_fpath] = "excluded by pattern"
                    continue
                _snapshot_file(project, fpath, rel_fpath, files_dir, files, skipped, escaped)
    if escaped:
        shutil.rmtree(extraction_dir, ignore_errors=True)
        raise ValidationError(
            "Snapshot aborted: symlink escape(s) inside scope point outside the project: "
            + ", ".join(sorted(escaped))
        )
    metadata: dict[str, Any] = {
        "version": ISOLATION_VERSION,
        "execution_id": execution_id,
        "created_at": _now(),
        "project": str(project),
        "root": str(isolation_root),
        "snapshot_dir": str(extraction_dir),
        "scope": sorted(rel for _abs, rel in scope_resolved),
        "exclusions": exclusion_patterns,
        "reservations": list(reservations or []),
        "requires_worktree_alternative": True,  # marker of the workflow contract
        "scope_kinds": {rel: ("file" if _abs.is_file() else "dir") for _abs, rel in scope_resolved},
        "files": dict(sorted(files.items())),
        "skipped": dict(sorted(skipped.items())),
    }
    atomic_write(extraction_dir / "snapshot.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    # Trust binding: authoritative registry record for THIS checkout, keyed by
    # execution ID. Written under the registry lock; integrate never trusts the
    # editable snapshot.json for anything security-relevant.
    with file_lock(default_isolation_root(project) / "registry"):
        registry = _load_registry(project)
        record = registry["executions"].setdefault(
            execution_id, {"created_at": _now(), "reservations": list(reservations or [])}
        )
        record["project"] = str(project)
        record["snapshot"] = str(extraction_dir)
        record["scope"] = metadata["scope"]
        record["scope_kinds"] = metadata["scope_kinds"]
        record["base_manifest"] = metadata["files"]
        _save_registry(project, registry)
    metadata["file_count"] = len(files)
    return metadata


def _snapshot_file(
    project: Path,
    fpath: Path,
    rel: str,
    files_dir: Path,
    files: dict[str, dict[str, Any]],
    skipped: dict[str, str],
    escaped: list[str],
) -> None:
    st = os.lstat(fpath)
    if stat.S_ISLNK(st.st_mode):
        if _inside(project, fpath):
            skipped[rel] = "symlink to project file (copied as regular file)"
            fpath = fpath.resolve()  # copy content, never the link
        else:
            escaped.append(rel)
            return
    elif not stat.S_ISREG(st.st_mode):
        # FIFO/socket/device: never opened, so a snapshot cannot hang on reads.
        skipped[rel] = "non-regular file (fifo/socket/device) not copied"
        return
    data = fpath.read_bytes()
    dest = files_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    mode = _file_mode(fpath)
    os.chmod(dest, mode)  # env copy carries the intended mode from day one
    files[rel] = {
        "hash": _hash_bytes(data),
        "mode": mode,
    }


# --------------------------------------------------------------------------
# Reading metadata / listing
# --------------------------------------------------------------------------


def load_metadata(project: Path, snapshot: str) -> dict[str, Any]:
    """Load snapshot metadata by id (default root) or by explicit path.

    NOTE: the returned metadata is convenient but NOT trusted; use
    :func:`load_binding` (or call ``integrate_snapshot``) for anything that
    must be authorized against the project-local registry.
    """
    candidate = Path(snapshot)
    if candidate.exists() and (candidate / "snapshot.json").exists():
        metadata_path = candidate / "snapshot.json"
    else:
        metadata_path = default_isolation_root(project).resolve() / snapshot / "snapshot.json"
    if not metadata_path.exists():
        raise ValidationError(f"Snapshot not found: {snapshot}")
    try:
        with metadata_path.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Corrupt snapshot metadata at {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("version") != ISOLATION_VERSION:
        raise ValidationError(f"Unsupported snapshot metadata at {metadata_path}")
    return metadata


def load_binding(project: Path, snapshot: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind a snapshot to the project-local registry trust record.

    Returns ``(metadata, trusted_record)``. Refuses when:
    - the snapshot was created for a DIFFERENT project checkout
      (:class:`ConflictError`), or
    - the execution is not registered in the destination project's registry
      (:class:`ConflictError`), or
    - the registered snapshot path disagrees with the metadata
      (:class:`ValidationError`).

    The trusted record's ``base_manifest`` and ``scope`` override whatever the
    editable ``snapshot.json`` says, so tampering with the snapshot directory
    cannot widen scope or fabricate entries.
    """
    project = project.resolve()
    metadata = load_metadata(project, snapshot)
    if metadata.get("project") != str(project):
        raise ConflictError(
            f"Snapshot '{metadata.get('execution_id', snapshot)}' belongs to project "
            f"{metadata.get('project')!r}, not the requested checkout {str(project)!r}; "
            "snapshots can only be integrated into the checkout that created them"
        )
    registry = _load_registry(project)
    execution_id = metadata["execution_id"]
    record = registry["executions"].get(execution_id)
    if record is None:
        raise ConflictError(
            f"Execution '{execution_id}' is not registered in this checkout's isolation "
            "registry; refusing to integrate an unbound snapshot"
        )
    base_manifest = record.get("base_manifest")
    if not isinstance(base_manifest, dict):
        raise ValidationError(
            f"Registry record for '{execution_id}' has no trusted base manifest"
        )
    if record.get("snapshot") != metadata.get("snapshot_dir"):
        raise ValidationError(
            f"Registry snapshot path for '{execution_id}' does not match the metadata: "
            f"{record.get('snapshot')!r} != {metadata.get('snapshot_dir')!r}"
        )
    metadata["scope"] = list(record["scope"])
    metadata["files"] = dict(record["base_manifest"])
    metadata["scope_kinds"] = _scope_kinds(metadata["scope"], record, metadata["files"])
    return metadata, record


def list_snapshots(project: Path, root: Path | None = None) -> list[dict[str, Any]]:
    """List snapshots known to the default (or given) isolation root.

    Corrupt metadata raises a clean :class:`ValidationError` instead of leaking
    raw decode errors.
    """
    isolation_root = (root or default_isolation_root(project)).resolve()
    out: list[dict[str, Any]] = []
    if isolation_root.exists():
        for entry in sorted(isolation_root.iterdir()):
            metadata_path = entry / "snapshot.json"
            if metadata_path.exists():
                try:
                    with metadata_path.open(encoding="utf-8") as stream:
                        metadata = json.load(stream)
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        f"Corrupt snapshot metadata at {metadata_path}: {exc}"
                    ) from exc
                try:
                    files = len(metadata.get("files", {}))
                    created_at = metadata["created_at"]
                    execution_id = metadata["execution_id"]
                    scope = metadata.get("scope", [])
                    hashed_root = metadata.get("root", str(isolation_root))
                except (KeyError, AttributeError, TypeError) as exc:
                    raise ValidationError(
                        f"Corrupt snapshot metadata at {metadata_path}: {exc}"
                    ) from exc
                out.append(
                    {
                        "execution_id": execution_id,
                        "created_at": created_at,
                        "scope": scope,
                        "files": files,
                        "has_journal": bool(list(entry.glob(".journals/*/journal.json"))),
                        "root": hashed_root,
                    }
                )
    return out


# --------------------------------------------------------------------------
# Integration (preflight + staging + local journal; NOT the durable
# transactions API -- see module docstring).
# --------------------------------------------------------------------------

JOURNAL_NAME = "journal.json"


def _scope_kinds(trusted_scope: list[str], record: dict[str, Any], files: dict[str, Any]) -> dict[str, str]:
    """Kind (file/dir) per trusted scope root.

    Prefer the explicit record from snapshot time; fall back to inspecting the
    trusted base manifest (a single-file root is always one of its keys; a
    directory root is never a key).
    """
    kinds = record.get("scope_kinds")
    if isinstance(kinds, dict):
        return {r: ("file" if kinds.get(r) == "file" else "dir") for r in trusted_scope}
    return {r: ("file" if r in files else "dir") for r in trusted_scope}


def _effective_roots(
    trusted_scope: list[str],
    kinds: dict[str, str],
    requested: list[str] | None,
) -> list[tuple[str, str]]:
    """Integrate scope may only NARROW the trusted snapshot scope.

    Returns [(root, kind)]: exact-file roots may only be selected as-is and
    strictly match that single file; directory roots additionally match every
    descendant (exact file OR directory descendant, consistently, both above).
    """
    if not requested:
        return [(root if root not in ("", ".") else ".", kinds.get(root, "dir"))
                for root in trusted_scope]
    roots: list[tuple[str, str]] = []
    for item in requested:
        rel = PurePosixPath(item.replace(os.sep, "/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError(f"Integrate scope escapes the project: {item!r}")
        norm = rel.as_posix().rstrip("/")
        for root in trusted_scope:
            kind = kinds.get(root, "dir")
            if root in ("", "."):
                roots.append((".", "dir"))
                break
            if not (norm == root or (kind == "dir" and norm.startswith(root.rstrip("/") + "/"))):
                continue
            # A file root may only be taken exactly; narrowing "under" a file
            # root can never exist.
            if kind == "file" and norm != root:
                continue
            roots.append((norm, kind if norm == root else "dir"))
            break
        else:
            raise ValidationError(
                f"Integrate scope {item!r} is outside the trusted snapshot scope "
                f"{trusted_scope}; scope may only be narrowed, never widened"
            )
    return roots


def _in_scope(rel: str, roots: list[tuple[str, str]]) -> bool:
    """Exact-file OR directory-descendant matching, shared by every scope check."""
    for root, kind in roots:
        if root in ("", "."):
            return True
        if kind == "file":
            if rel == root:
                return True
        elif rel == root or rel.startswith(root + "/"):
            return True
    return False


def _env_state(files_dir: Path, rel: str) -> dict[str, Any]:
    """Inspect the environment copy of one file.

    Returns {"kind": "missing"|"regular"|"offset", "hash": str|None,
    "mode": int|None}. Directory symlinks point INSIDE the environment resolve
    to their target copy; anything else (symlink escape, FIFO, socket, device)
    is "offset" (ignored, never read) so integration can never hang or leak.
    """
    path = files_dir / rel
    try:
        st = os.lstat(path)
    except OSError:
        return {"kind": "missing", "hash": None, "mode": None}
    if stat.S_ISLNK(st.st_mode):
        resolved = path.resolve()
        if _inside(files_dir, resolved):
            return {
                "kind": "regular",
                "hash": _sha256_file(resolved),
                "mode": resolved.stat().st_mode & 0o777,
            }
        return {"kind": "offset", "hash": None, "mode": None}
    if not stat.S_ISREG(st.st_mode):
        return {"kind": "offset", "hash": None, "mode": None}
    return {"kind": "regular", "hash": _sha256_file(path), "mode": _file_mode(path)}


def _plan_candidates(
    metadata: dict[str, Any],
    roots: list[tuple[str, str]],
    resume_applied: set[str],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Resolve the change plan from the trusted baseline vs. environment state.

    Trusted manifest (``metadata["files"]``, from the registry -- NOT the
    editable snapshot.json) defines the baseline. Scope matching is exact-file
    OR directory-descendant (``_in_scope``), so a single-file snapshot scope
    like ``['src/only.py']`` behaves symmetrically with a directory scope.
    Classification against the baseline:
    - ``write``: env content hash or permission mode differs from baseline;
    - ``delete``: trusted-manifest file missing from the environment;
    - new files: regular environment files absent from the manifest;
    - non-regular/escaping environment entries are ignored (never read);
    - everything else is left alone.

    Returns (plan entries [{rel, action, reason, mode}], unchanged count,
    ignored rels).
    """
    files_dir = Path(metadata["snapshot_dir"]) / "files"
    files = metadata["files"]

    plan: list[dict[str, Any]] = []
    unchanged = 0
    ignored: list[str] = []
    for rel in sorted(files):
        if not _in_scope(rel, roots):
            continue
        if rel in resume_applied:
            continue  # already integrated by a previous interrupted run
        state = _env_state(files_dir, rel)
        base = files[rel]
        if state["kind"] == "missing":
            plan.append({"rel": rel, "action": "delete", "reason": "deleted in environment", "mode": None})
            continue
        if state["kind"] == "offset":
            ignored.append(rel)
            continue
        if state["hash"] != base["hash"] or state["mode"] != base.get("mode"):
            plan.append(
                {"rel": rel, "action": "write", "reason": "changed in environment", "mode": state["mode"]}
            )
        else:
            unchanged += 1

    # New files created in the environment (trusted manifest says they did not
    # exist at baseline). Only regular files are candidates; symlink/FIFO and
    # other exotic entries in the environment are ignored, never read. For a
    # single-file scope root, files "below" that root can never be candidates.
    for env_rel, kind in _walk_env(files_dir):
        if not _in_scope(env_rel, roots) or env_rel in files or env_rel in resume_applied:
            continue
        state = _env_state(files_dir, env_rel)
        if state["kind"] != "regular":
            ignored.append(env_rel)
            continue
        plan.append(
            {"rel": env_rel, "action": "write", "reason": "new in environment", "mode": state["mode"]}
        )
    return plan, unchanged, ignored


def _walk_env(files_dir: Path) -> list[tuple[str, str]]:
    """List environment entries: (rel, kind) for regular/symlink/other."""
    found: list[tuple[str, str]] = []
    if not files_dir.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(files_dir, followlinks=False):
        current = Path(dirpath)
        for fname in filenames:
            rel = _as_posix((current / fname).relative_to(files_dir))
            st = os.lstat(current / fname)
            kind = "symlink" if stat.S_ISLNK(st.st_mode) else "regular" if stat.S_ISREG(st.st_mode) else "other"
            found.append((rel, kind))
    return found


def _dest_state(project: Path, rel: str) -> dict[str, Any]:
    path = project / rel
    if not path.exists():
        return {"kind": "missing", "hash": None, "mode": None}
    if not path.is_file():
        return {"kind": "offset", "hash": None, "mode": None}
    return {"kind": "regular", "hash": _sha256_file(path), "mode": _file_mode(path)}


def _conflicts(
    project: Path, metadata: dict[str, Any], plan: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Overlap/part-way conflict resolution against the trusted baseline.

    Returns (conflict rels, idempotent rels removed from the plan, ignored
    destination-side entries). An entry is IDEMPOTENT (no-op) when the
    destination already equals the environment exactly (content AND mode);
    such reruns never overwrite later destination edits — those surface as
    conflicts instead. A destination-side change that diverges from the
    environment while the environment also changed vs. baseline is a conflict:
    - changed-in-env vs changed-in-dest (classic overlap),
    - new-in-env vs independently added in dest ("new vs new").
    """
    conflicts: list[str] = []
    idempotent: list[str] = []
    files = metadata["files"]
    for entry in list(plan):
        rel = entry["rel"]
        dest = _dest_state(project, rel)
        base = files.get(rel)
        if entry["action"] == "delete":
            if dest["kind"] == "missing":
                idempotent.append(rel)  # already gone at the destination
                plan.remove(entry)
            elif base is not None and (dest["hash"] != base["hash"] or dest["mode"] != base.get("mode")):
                conflicts.append(rel)  # dest modified; env deleted -> overlap
            # else: dest still equals baseline -> honour the deletion
            continue
        # action == write
        if base is None:
            if dest["kind"] == "regular" and entry.get("mode") is not None:
                if dest["hash"] is not None and _env_state(
                    Path(metadata["snapshot_dir"]) / "files", rel
                )["hash"] == dest["hash"] and dest["mode"] == entry["mode"]:
                    idempotent.append(rel)
                    plan.remove(entry)
                else:
                    conflicts.append(rel)  # new vs new: both sides added it differently
            continue
        if dest["kind"] == "missing":
            continue  # recreate; record as apply
        dest_changed = dest["hash"] != base["hash"] or dest["mode"] != base.get("mode")
        if not dest_changed:
            continue  # dest untouched -> honour the environment change
        if base is not None and dest["hash"] is not None:
            env_now = _env_state(Path(metadata["snapshot_dir"]) / "files", rel)
            if (
                env_now["kind"] == "regular"
                and env_now["hash"] == dest["hash"]
                and env_now["mode"] == dest["mode"]
            ):
                idempotent.append(rel)  # dest already matches the environment
                plan.remove(entry)
                continue
        conflicts.append(rel)
    return sorted(set(conflicts)), sorted(set(idempotent)), []


def _check_dest_path(project: Path, rel: str) -> None:
    """Reject destination path escapes before any write.

    Policy: parent directories must resolve inside the project (a symlinked
    parent pointing outside is an escape and is refused). The leaf is replaced
    only atomically via ``os.replace`` below: that substitutes the symlink
    itself with a regular file and never writes *through* it, so a leaf
    symlink is allowed and documented, not an escape.
    """
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValidationError(f"Destination path escape rejected: {rel!r}")
    base_real = project.resolve()
    target = project / Path(*p.parts)
    parent_real = target.parent.resolve()
    if parent_real != base_real and not _inside(base_real, parent_real):
        raise ValidationError(f"Destination parent escapes project: {rel!r}")
    if target.exists() and not target.is_file() and not target.is_symlink():
        raise ValidationError(f"Destination is not a regular file: {rel!r}")


def _stage(
    metadata: dict[str, Any], plan: list[dict[str, Any]], integrate_id: str
) -> tuple[Path, dict[str, Any]]:
    """Copy candidate contents into a local journal dir under the snapshot.

    The environment is NEVER modified during integration; staging copies data
    out, so a failure anywhere below preserves the unintegrated environment.
    """
    files_dir = Path(metadata["snapshot_dir"]) / "files"
    journal_dir = Path(metadata["snapshot_dir"]) / ".journals" / integrate_id
    staged = journal_dir / "files"
    staged.mkdir(parents=True, exist_ok=True)
    pending: list[dict[str, Any]] = []
    for entry in plan:
        staged_rel = staged / entry["rel"]
        if entry["action"] == "write":
            staged_rel.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(files_dir / entry["rel"], staged_rel)
        pending.append({"rel": entry["rel"], "action": entry["action"]})
    journal = {
        "version": 1,
        "id": integrate_id,
        "created_at": _now(),
        "status": "in_progress",
        "applied": [],
        "pending": pending,
    }
    atomic_write(journal_dir / JOURNAL_NAME, json.dumps(journal, indent=2, sort_keys=True) + "\n")
    return journal_dir, journal


def _find_journal(snapshot_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    """Locate the single local integration journal, if any, and enforce its
    lifecycle:

    - a journal whose plan is fully accounted for (no pending entries left, or
      every pending rel already recorded as applied) is FINALIZED in place and
      returns None: a crash between the last apply and cleanup, or a resume
      whose residual plan became empty, must never block future integrations;
    - a genuinely incomplete journal raises :class:`IntegrationInterrupted`
      unless the caller resumes.
    """
    journals_root = snapshot_dir / ".journals"
    if not journals_root.exists():
        return None
    for journal_path in sorted(journals_root.glob(f"*/{JOURNAL_NAME}")):
        try:
            with journal_path.open(encoding="utf-8") as stream:
                journal = json.load(stream)
        except (OSError, json.JSONDecodeError):
            continue
        if journal.get("status") != "in_progress":
            continue
        applied = {item["rel"] for item in journal.get("applied", [])}
        pending = [item for item in journal.get("pending", []) if item["rel"] not in applied]
        if not pending:
            shutil.rmtree(journal_path.parent, ignore_errors=True)  # finalize
            continue
        return journal_path.parent, journal
    return None


def _apply_one(project: Path, staging_dir: Path, entry: dict[str, Any]) -> None:
    """Apply one journaled change atomically. Called per file; test seam.

    The applied file's recorded permission mode is restored before the atomic
    rename, so executables stay executable and private stays private (defaults
    to 0o644 for entries without a recorded mode).
    """
    rel = entry["rel"]
    _check_dest_path(project, rel)
    dest = project / rel
    if entry["action"] == "delete":
        if dest.exists():
            dest.unlink()
        return
    src = staging_dir / "files" / rel
    data = src.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.dado-integrate-{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, entry.get("mode") or 0o644)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def integrate_snapshot(
    project: Path,
    snapshot: str,
    *,
    scope: list[str] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Integrate the environment's relevant changed files into the project.

    Preflight (all-or-nothing): bind the snapshot to the destination project's
    registry trust record (``load_binding``), narrow the scope (never widened),
    and compare each trusted baseline file against the current destination;
    overlapping changes (environment changed AND destination changed since the
    snapshot, to different content) raise :class:`ConflictError` with the list
    and change nothing. Idempotent reruns (destination already equals the
    environment) are clean no-ops that never overwrite later edits. Output is
    confined to the declared scope; symlink and ``..`` path escapes are
    rejected; FIFO/socket/device entries are ignored.

    On interruption, raises :class:`IntegrationInterrupted` with applied and
    remaining lists; the environment stays untouched. Re-run with
    ``resume=True`` to continue from the local journal (which is finalized
    when the residual plan is empty), or use ``discard_journal`` for the
    explicit reconcile action.

    The result includes ``requires_verification: True`` when files changed:
    integration returning ``applied`` is NOT a verification claim; verification
    is a separate, mandatory step afterwards.
    """
    project = project.resolve()
    metadata, _record = load_binding(project, snapshot)
    snapshot_dir = Path(metadata["snapshot_dir"])
    trusted_scope = metadata["scope"]
    roots = _effective_roots(trusted_scope, metadata["scope_kinds"], scope)
    resume_applied: set[str] = set()
    existing = _find_journal(snapshot_dir)
    if existing is not None:
        journal_dir, journal = existing
        if not resume:
            raise IntegrationInterrupted(
                f"Incomplete integration journal '{journal['id']}' for snapshot "
                f"'{metadata['execution_id']}': call integrate_snapshot(resume=True) "
                "to continue, or discard_journal() to reconcile."
            )
        integrate_id = journal["id"]
        resume_applied = {item["rel"] for item in journal.get("applied", [])}
    else:
        integrate_id = new_execution_id()

    plan, unchanged, ignored = _plan_candidates(metadata, roots, resume_applied)
    if not plan:
        result = {
            "snapshot": metadata["execution_id"],
            "applied": [],
            "deleted": [],
            "unchanged": unchanged,
            "idempotent": 0,
            "ignored": ignored,
            "requires_verification": False,
            "note": (
                "residual integration plan empty; journal finalized"
                if resume_applied
                else "no relevant changed files in declared scope"
            ),
        }
        if resume_applied:
            result["applied"] = sorted(resume_applied)  # reported as resume outcome
        return result

    conflicts, idempotent, _ignored_dest = _conflicts(project, metadata, plan)
    if conflicts:
        raise ConflictError(
            "Overlapping changes since snapshot; nothing integrated: " + ", ".join(conflicts)
        )
    for entry in plan:
        _check_dest_path(project, entry["rel"])  # preflight path-escape guard
    if not plan:
        return {
            "snapshot": metadata["execution_id"],
            "applied": [],
            "deleted": [],
            "unchanged": unchanged,
            "idempotent": len(idempotent),
            "ignored": ignored,
            "requires_verification": False,
            "note": "already integrated; idempotent no-op; later destination edits retained",
        }

    journal_dir, journal = _stage(metadata, plan, integrate_id)
    applied: list[str] = []
    deleted: list[str] = []
    failing: str = plan[0]["rel"] if plan else "?"
    try:
        for entry in plan:
            failing = entry["rel"]
            _apply_one(project, journal_dir, entry)
            journal["applied"].append({"rel": entry["rel"], "action": entry["action"]})
            journal["pending"] = [
                item for item in journal["pending"] if item["rel"] != entry["rel"]
            ]
            atomic_write(journal_dir / JOURNAL_NAME, json.dumps(journal, indent=2, sort_keys=True) + "\n")
            (applied if entry["action"] == "write" else deleted).append(entry["rel"])
    except (OSError, ValidationError, RuntimeError) as exc:
        raise IntegrationInterrupted(
            f"Integration interrupted after {len(applied)} apply step(s); "
            f"failing file {failing!r}: {exc}; environment preserved; "
            f"resume with integrate_snapshot(..., resume=True)"
        ) from exc
    # Journal complete; cleanup failure is not an integration failure (the
    # next run auto-finalizes a fully-accounted journal).
    try:
        shutil.rmtree(journal_dir)
    except OSError:
        pass
    return {
        "snapshot": metadata["execution_id"],
        "applied": applied,
        "deleted": deleted,
        "unchanged": unchanged,
        "idempotent": len(idempotent),
        "ignored": ignored,
        "requires_verification": bool(applied or deleted),
        "note": "integrated; verification is a separate mandatory step",
    }


def resume_journal(project: Path, snapshot: str) -> dict[str, Any]:
    """Reconcile an interrupted integration by resuming its journal."""
    return integrate_snapshot(project, snapshot, resume=True)


def discard_journal(project: Path, snapshot: str) -> dict[str, Any]:
    """Explicit reconcile action: discard a local incomplete journal.

    Trust binding applies: the snapshot must belong to THIS project checkout
    and match its registry record exactly (``load_binding``), so the trusted
    snapshot path -- never the editable metadata pointer -- decides which
    ``.journals`` directory is removed.

    The interrupted run's applied steps remain in the destination (recorded in
    the journal); discarding does NOT undo them. Callers should verify state
    after discarding, which is why the result carries requires_verification.
    """
    metadata, _record = load_binding(project, snapshot)
    journals_root = Path(metadata["snapshot_dir"]) / ".journals"
    removed: list[str] = []
    if journals_root.exists():
        for journal_dir in sorted(journals_root.iterdir()):
            if (journal_dir / JOURNAL_NAME).exists():
                try:
                    with (journal_dir / JOURNAL_NAME).open(encoding="utf-8") as stream:
                        journal = json.load(stream)
                    removed.append(journal.get("id", journal_dir.name))
                except (OSError, json.JSONDecodeError):
                    removed.append(journal_dir.name)
        shutil.rmtree(journals_root, ignore_errors=True)
    return {
        "snapshot": metadata["execution_id"],
        "discarded": removed,
        "requires_verification": True,
    }


# --------------------------------------------------------------------------
# CLI-compatible entry point (wired later as `dado isolate ...`)
# --------------------------------------------------------------------------


def _run(args: argparse.Namespace, out: list[str]) -> int:
    project = Path(args.project or ".").resolve()
    if args.command == "create":
        metadata = create_snapshot(
            project,
            list(args.scope or []),
            root=Path(args.to).resolve() if args.to else None,
            execution_id=args.id,
            exclusions=list(args.exclude) if args.exclude else None,
            reservations=list(args.reserve) if args.reserve else None,
        )
        out.append(
            json.dumps(
                {
                    "execution_id": metadata["execution_id"],
                    "snapshot_dir": metadata["snapshot_dir"],
                    "files": len(metadata["files"]),
                    "scope": metadata["scope"],
                    "requires_verification": False,
                }
            )
        )
        return 0
    if args.command == "list":
        out.append(json.dumps({"snapshots": list_snapshots(project, Path(args.dir).resolve() if args.dir else None)}))
        return 0
    if args.command == "integrate":
        result = integrate_snapshot(project, args.snapshot, scope=list(args.scope or []) or None, resume=args.resume)
        out.append(json.dumps(result))
        return 0
    raise ValidationError(f"Unknown command: {args.command}")


def main(argv: list[str]) -> int:
    """CLI entry: ``isolate create|list|integrate``. Prints JSON; returns code."""
    parser = argparse.ArgumentParser(prog="dado isolate", description="Local isolated environments")
    sub = parser.add_subparsers(dest="command", required=True)
    p_create = sub.add_parser("create", help="snapshot declared scope of current checkout")
    p_create.add_argument("--project", default=".")
    p_create.add_argument("--scope", action="append", default=[])
    p_create.add_argument("--to", default=None, help="snapshot root (default: .dado/local/isolation)")
    p_create.add_argument("--id", default=None)
    p_create.add_argument("--exclude", action="append", default=[])
    p_create.add_argument("--reserve", action="append", default=[], help="resource label, e.g. port:4319")
    p_list = sub.add_parser("list", help="list local snapshots")
    p_list.add_argument("--project", default=".")
    p_list.add_argument("--dir", default=None)
    p_integrate = sub.add_parser("integrate", help="integrate changed files back into the project")
    p_integrate.add_argument("--project", default=".")
    p_integrate.add_argument("--snapshot", required=True)
    p_integrate.add_argument("--scope", action="append", default=[])
    p_integrate.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    out: list[str] = []
    try:
        return _run(args, out)
    except (DadoError, OSError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        for line in out:
            print(line)
