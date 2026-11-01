"""Bounded local evidence capture and verification for DADO work.

This module builds and verifies *local* evidence artifacts. It never talks to
the network and never auto-executes anything: the only function that spawns a
process is ``capture_check``, and it runs exactly the argv the caller passed —
never a shell string. Verification functions only read.

API for integrators
-------------------
``snapshot(project, scopes=None, *, git_timeout=10.0, exclude_dirs=None, exclude_paths=None)``
    Deterministic content digest over the project's code, including dirty and
    untracked files and deleted (tracked) relevant files, while excluding
    ``.git``, DADO execution metadata (``.dado``), build caches, git-ignored
    generated output and secret-looking paths. Symlinks are hashed as link
    targets, never followed. When git is unavailable or a git probe exceeds
    ``git_timeout``, the module falls back to a plain filesystem walk
    (deleted-file detection then loses git's insight and is recorded as such in
    the snapshot's ``git`` field).

``capture_check(project, evidence_dir, command, criterion=None, *, cwd=None,
    timeout=120.0, output_cap_bytes=1_000_000, scopes=None, session=None,
    git_timeout=10.0, record_id=None, exclude_dirs=None)``
    Explicit opt-in. Takes a snapshot before and after running the
    caller-supplied argv once (no shell); any relevant code change during the
    run rejects the attempt with ``ConflictError`` (changes to ignored files,
    e.g. caches a read-only verifier produces, are allowed). On success it
    persists a self-hashing record with the command, cwd, duration, exit code,
    bounded log references and the declared criterion. All snapshot options
    (``exclude_dirs``, ``exclude_paths``, ``git_timeout``) are persisted in
    ``record["declared"]`` and replayed by verification. Output is bounded at
    generation time: log files stop growing at ``output_cap_bytes`` — no
    unbounded temp files. The child runs in its own session/group and is
    killed as a group on timeout.

``verify_record(record, *, project=None, check_code=True)``
    Revalidates a record: structural schema/version checks, self-hash
    integrity, log hashes (rejecting a tampered log) and — unless
    ``check_code=False`` — a live code-fingerprint comparison that rejects code
    changed since capture; the report names added, deleted or modified files.
    ``ConflictError`` means rejection; ``ValidationError`` means the record is
    structurally unusable.

A record is a JSON document of kind ``dado/evidence/check-record``
(``schema_version`` 1) that carries its own manifest: every log file reference
carries a sha256 digest and byte length, and ``record["integrity"]`` carries
the digest of the record body itself (see ``record_integrity_hash``).

Recommended placement for evidence is the DADO work folder (for example
``.dado/work/active/<work>/evidence``), which snapshots already exclude.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import signal
import stat as stat_module
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConflictError, DadoError, ValidationError
from .fs import atomic_write

__all__ = [
    "SNAPSHOT_VERSION",
    "RECORD_VERSION",
    "RECORD_KIND",
    "snapshot",
    "capture_check",
    "verify_record",
    "load_record",
    "record_integrity_hash",
]

SNAPSHOT_VERSION = 1
RECORD_VERSION = 1
RECORD_KIND = "dado/evidence/check-record"

READ_CHUNK = 65536
POLL_INTERVAL = 0.02
JOIN_GRACE = 5.0
DEFAULT_GIT_TIMEOUT = 10.0
DEFAULT_TIMEOUT = 120.0
DEFAULT_OUTPUT_CAP = 1_000_000
_DETAIL_LIMIT = 20  # items quoted verbatim in error messages
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

# Excluded everywhere, parts of DADO execution metadata, tooling or caches.
_BASE_EXCLUDED_DIRS = frozenset((
    ".git", ".hg", ".svn", ".dado",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".hypothesis", ".eggs", ".venv", "venv",
    ".cache",
))
# File or directory names that look like secrets; their content is never read.
_SECRET_NAME_PATTERNS = (
    ".env*",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", ".npmrc", ".secrets", "secrets",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_secret_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_NAME_PATTERNS)


def _excluded_rel(rel: str, excluded_dirs: frozenset[str]) -> bool:
    parts = PurePosixPath(rel).parts
    return any(part in excluded_dirs or _is_secret_name(part) for part in parts)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _entry_for_file(path: Path) -> dict[str, Any] | None:
    try:
        target = os.fsdecode(os.readlink(path))
        return {
            "kind": "symlink",
            "target": target,
            "bytes": len(target.encode("utf-8")),
            "sha256": _sha_hex(target.encode("utf-8")),
        }
    except OSError:
        pass
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = stream.read(READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return {"kind": "file", "bytes": size, "sha256": digest.hexdigest()}


def _diff_files(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable changes between two ``files`` fingerprint mappings."""
    changes: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        if old == new:
            continue
        if old is None:
            changes.append(f"added {rel}")
        elif new is None:
            changes.append(f"deleted {rel}")
        else:
            changes.append(f"modified {rel}")
    return changes


def _raise_conflict(what: str, changes: list[str]) -> None:
    quoted = "; ".join(changes[:_DETAIL_LIMIT])
    if len(changes) > _DETAIL_LIMIT:
        quoted += f"; … and {len(changes) - _DETAIL_LIMIT} more"
    raise ConflictError(f"{what}: {quoted}")


def _normalize_scopes(root: Path, scopes: Any) -> list[str]:
    if scopes is None:
        return []
    if isinstance(scopes, (str, Path, PurePosixPath)):
        items: list[Any] = [scopes]
    elif isinstance(scopes, (list, tuple, set, frozenset)) and all(
            isinstance(item, (str, Path, PurePosixPath)) for item in scopes):
        items = list(scopes)
    else:
        raise ValidationError("scopes must be a path or a list of paths")
    normalized: list[str] = []
    for item in items:
        text = str(item)
        if not text.strip():
            raise ValidationError("empty scope path")
        raw = Path(text)
        absolute = raw if raw.is_absolute() else root / raw
        try:
            resolved = absolute.resolve(strict=False)
        except OSError as exc:
            raise ValidationError(f"cannot resolve scope {text!r}: {exc}") from exc
        if resolved == root or root in resolved.parents:
            rel = "." if resolved == root else resolved.relative_to(root).as_posix()
        else:
            raise ValidationError(f"scope escapes the project: {text!r}")
        if rel == ".":
            continue  # a scope of the project root is simply "everything"
        if rel not in normalized:
            normalized.append(rel)
    return sorted(normalized)


def _validate_argv(command: Any) -> list[str]:
    if isinstance(command, str):
        raise ValidationError("command must be an argv list, not a shell string")
    if isinstance(command, (list, tuple)):
        argv = command
    else:
        raise ValidationError("command must be a list of argv strings")
    if not argv:
        raise ValidationError("command argv is empty")
    words: list[str] = []
    for part in argv:
        if isinstance(part, str):
            word = part
        else:
            try:
                word = os.fspath(part)
            except TypeError as exc:
                raise ValidationError("command argv must only contain strings") from exc
            if not isinstance(word, str):
                raise ValidationError("command argv must only contain strings")
        if word == "":
            raise ValidationError("command argv contains an empty element")
        words.append(word)
    return words


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class _GitFallback(Exception):
    """Internal signal: fall back to a filesystem walk with a reason."""


def _run_git(project: Path, args: list[str], timeout: float) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _GitFallback("timeout") from exc
    except FileNotFoundError as exc:
        raise _GitFallback("git-missing") from exc
    except (OSError, ValueError) as exc:
        raise _GitFallback("git-error") from exc
    if completed.returncode != 0:
        raise _GitFallback("git-error")
    return completed.stdout


def _git_lists(project: Path, scopes: list[str], timeout: float) -> tuple[set[str], set[str]]:
    inside = _run_git(project, ["rev-parse", "--is-inside-work-tree"], timeout)
    if inside.split() != [b"true"]:
        raise _GitFallback("not-a-work-tree")
    pathspec = ["--"] + scopes if scopes else []
    status = _run_git(project, ["status", "--porcelain=v1", "-z", "--untracked-files=normal", *pathspec], timeout)
    tracked = _run_git(project, ["ls-files", "-z", *pathspec], timeout)
    ignored = _run_git(project, ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", *pathspec], timeout)

    missing: set[str] = set()
    records = status.split(b"\0")
    index = 0
    while index < len(records):
        field = records[index]
        index += 1
        if not field:
            continue
        state = field[:2].decode("ascii", "replace")
        path = os.fsdecode(field[3:])
        if state[0] in "RC":  # rename/copy: the next NUL field is the source path
            if index < len(records):
                missing.add(os.fsdecode(records[index]))
                index += 1
        if "D" in state:
            missing.add(path)
    ignored_paths = {os.fsdecode(item).rstrip("/") for item in ignored.split(b"\0") if item}
    return missing, ignored_paths


def _excluded_prefix(rel: str, excluded_paths: tuple[str, ...]) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in excluded_paths)


def _ignored_rel(rel: str, ignored: set[str]) -> bool:
    """True when the path itself or one of its parent directories is git-ignored.

    ``git ls-files --others --ignored`` reports fully ignored directories once
    (``build/``), so parent-prefix matching is required to keep such trees out.
    """
    if rel in ignored:
        return True
    parts = rel.split("/")
    for index in range(1, len(parts)):
        if "/".join(parts[:index]) in ignored:
            return True
    return False


def _normalize_exclude_dirs(exclude_dirs: Any) -> tuple[str, ...]:
    """Validate a user-supplied ``exclude_dirs`` into a normalized name tuple.

    Entries are single directory *names* (no path separators, no ``.``/``..``);
    they are persisted into records and replayed verbatim at verify time.
    """
    if exclude_dirs is None:
        return ()
    if isinstance(exclude_dirs, str):
        raise ValidationError("exclude_dirs takes an iterable of directory names, not one string")
    try:
        items = list(exclude_dirs)
    except TypeError as exc:
        raise ValidationError("exclude_dirs must be an iterable of directory names") from exc
    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValidationError("exclude_dirs entries must be directory-name strings")
        name = item.strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValidationError(f"invalid exclude_dirs directory name: {item!r}")
        if name not in normalized:
            normalized.append(name)
    return tuple(sorted(normalized))


def snapshot(
    project: str | os.PathLike[str],
    scopes: Any = None,
    *,
    git_timeout: float = DEFAULT_GIT_TIMEOUT,
    exclude_dirs: Any = None,
    exclude_paths: Any = None,
) -> dict[str, Any]:
    """Deterministic content fingerprint (with ``digest``) of the code tree.

    ``scopes`` optionally restricts the fingerprint to paths (files or
    directories) inside ``project``. Deleted tracked files appear as
    ``{"kind": "missing"}`` entries; untracked and dirty files are hashed as
    they exist on disk, so the digest covers exactly what verification would
    see again later. ``digest`` is computed only over the file map and its
    parameters; the ``git`` field is informational (a git availability change
    on its own does not change the digest). ``exclude_dirs`` selects whole
    directory *names* to skip anywhere in the tree; ``exclude_paths`` selects
    exact relative paths (subtrees) to skip entirely.
    """
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(f"project is not a directory: {root}")
    scope_list = _normalize_scopes(root, scopes)
    excluded = _BASE_EXCLUDED_DIRS | frozenset(_normalize_exclude_dirs(exclude_dirs))
    excluded_paths_list = _normalize_exclude_paths(exclude_paths)

    missing: set[str] = set()
    ignored: set[str] = set()
    git_info: dict[str, Any] = {"available": False, "mode": "filesystem-walk", "reason": None, "ignored_files": 0}
    try:
        missing, ignored = _git_lists(root, scope_list, git_timeout)
        git_info = {
            "available": True,
            "mode": "git",
            "reason": None,
            "ignored_files": len(ignored),
        }
    except _GitFallback as reason:
        git_info["reason"] = str(reason)

    files: dict[str, Any] = {}
    _walk_entries(root, scope_list, ignored, excluded, excluded_paths_list, files)
    for rel in sorted(missing):
        if rel not in files and not _excluded_rel(rel, excluded) \
                and not _excluded_prefix(rel, excluded_paths_list) and not _ignored_rel(rel, ignored):
            files[rel] = {"kind": "missing"}

    digest = _sha_hex(_canonical({"version": SNAPSHOT_VERSION, "scopes": scope_list, "files": files}))
    return {
        "version": SNAPSHOT_VERSION,
        "digest": digest,
        "root": str(root),
        "scopes": scope_list,
        "git": git_info,
        "files": files,
    }


def _normalize_exclude_paths(excluded_paths: Any) -> tuple[str, ...]:
    if excluded_paths is None:
        return ()
    if isinstance(excluded_paths, (str, os.PathLike)):
        excluded_paths = [excluded_paths]
    try:
        items = list(excluded_paths)
    except TypeError as exc:
        raise ValidationError("exclude_paths must be an iterable of paths") from exc
    normalized: list[str] = []
    for item in items:
        if isinstance(item, PurePosixPath):
            text = item.as_posix()
        elif isinstance(item, str):
            text = item
        elif isinstance(item, os.PathLike):
            text = os.fspath(item)
        else:
            raise ValidationError("exclude_paths must be path strings")
        if not text or str(text).strip() == "":
            raise ValidationError("empty exclude_paths entry")
        text = PurePosixPath(str(text).rstrip("/")).as_posix()
        if text in (".", ""):
            raise ValidationError("exclude_paths cannot exclude the whole project; use scopes instead")
        if text not in normalized:
            normalized.append(text)
    return tuple(sorted(normalized))


def _walk_entries(
    root: Path,
    scopes: list[str],
    ignored: set[str],
    excluded: frozenset[str],
    excluded_paths: tuple[str, ...],
    files: dict[str, Any],
) -> None:
    """Fill ``files`` with on-disk entries; never follows symlinks."""

    def record(full: Path, rel: str) -> None:
        if _excluded_prefix(rel, excluded_paths) or _excluded_rel(rel, excluded) or _ignored_rel(rel, ignored):
            return
        st = full.lstat()
        if stat_module.S_ISLNK(st.st_mode):
            files[rel] = _entry_for_file(full)
            return
        if stat_module.S_ISREG(st.st_mode):
            files[rel] = _entry_for_file(full)
        # sockets, fifos, devices: skipped silently

    def walk(top: Path, scope: str | None) -> None:
        st = top.lstat()
        if not stat_module.S_ISDIR(st.st_mode):
            record(top, scope)
            return
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            relative = Path(os.path.relpath(dirpath, root)).as_posix()
            prefix = "" if relative == "." else relative + "/"
            if _excluded_prefix(relative, excluded_paths) and relative != ".":
                dirnames.clear()
                filenames.clear()
            for name in list(dirnames):
                if name in excluded or _is_secret_name(name):
                    dirnames.remove(name)
                elif _excluded_prefix(prefix + name, excluded_paths):
                    dirnames.remove(name)
                elif os.path.islink(os.path.join(dirpath, name)):
                    dirnames.remove(name)  # do not descend into linked dirs
                    record(Path(dirpath) / name, prefix + name)
            for name in list(filenames):
                record(Path(dirpath) / name, prefix + name)

    if scopes:
        for scope in scopes:
            walk(root / PurePosixPath(scope), scope)
    else:
        walk(root, None)


# ---------------------------------------------------------------------------
# capture_check
# ---------------------------------------------------------------------------

def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(stream_obj: Any, log_file: Any, state: dict[str, int], cap: int) -> None:
    """Read a pipe to EOF, writing at most ``cap`` bytes to the log file.

    Excess output is discarded (never buffered, never expanded to disk), so a
    chatty child cannot grow unbounded memory or file size.
    """
    try:
        while True:
            chunk = stream_obj.read(READ_CHUNK)
            if not chunk:
                break
            room = cap - state["bytes"]
            if room <= 0:
                state["truncated"] = True
                continue
            if len(chunk) > room:
                log_file.write(chunk[:room])
                state["bytes"] += room
                state["truncated"] = True
            else:
                log_file.write(chunk)
                state["bytes"] += len(chunk)
    except OSError:
        pass
    finally:
        try:
            stream_obj.close()
        except OSError:
            pass


def _execute(
    argv: list[str],
    cwd: Path,
    timeout: float,
    output_cap: int,
    stdout_log: Path,
    stderr_log: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    stdout_state = {"bytes": 0, "truncated": False}
    stderr_state = {"bytes": 0, "truncated": False}
    with stdout_log.open("wb") as out_file, stderr_log.open("wb") as err_file:
        try:
            kwargs: dict[str, Any] = {"cwd": str(cwd), "stdin": subprocess.DEVNULL,
                                      "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            if hasattr(os, "setsid"):  # own session/group enables group-kill on timeout
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise DadoError(f"cannot run command {argv[0]!r}: {exc}") from exc
        readers = [
            threading.Thread(target=_drain, args=(handle, sink, state, output_cap), daemon=True)
            for handle, sink, state in (
                (proc.stdout, out_file, stdout_state),
                (proc.stderr, err_file, stderr_state),
            )
        ]
        for reader in readers:
            reader.start()
        deadline = started + max(timeout, 0.0)
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
        timed_out = proc.poll() is None
        if timed_out:
            _kill_group(proc)
        for reader in readers:
            reader.join(JOIN_GRACE)
        try:
            proc.wait(timeout=JOIN_GRACE)
        except subprocess.TimeoutExpired:  # pragma: no cover - extreme corner
            _kill_group(proc)
            proc.wait()
    duration = time.monotonic() - started
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 6),
        "stdout_bytes": stdout_state["bytes"],
        "stderr_bytes": stderr_state["bytes"],
        "output_truncated": bool(stdout_state["truncated"] or stderr_state["truncated"]),
    }


def capture_check(
    project: str | os.PathLike[str],
    evidence_dir: str | os.PathLike[str],
    command: Any,
    criterion: Any = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    output_cap_bytes: int = DEFAULT_OUTPUT_CAP,
    scopes: Any = None,
    session: dict[str, Any] | None = None,
    git_timeout: float = DEFAULT_GIT_TIMEOUT,
    record_id: str | None = None,
    exclude_dirs: Any = None,
) -> dict[str, Any]:
    """Run one caller-supplied check and persist a verified local evidence record.

    The command is passed as an argv list and executed without a shell. A code
    snapshot is taken before and after; changing relevant code during the run
    raises ``ConflictError`` (stale) and leaves no record behind. Otherwise the
    record is written into ``evidence_dir`` regardless of whether the criterion
    passed — a failing check is evidence too.
    """
    argv = _validate_argv(command)
    if criterion is None:
        criterion = {}
    if not isinstance(criterion, dict):
        raise ValidationError("criterion must be a mapping")
    declared_exit = criterion.get("exit_code")
    if not (declared_exit is None or isinstance(declared_exit, int) and not isinstance(declared_exit, bool)):
        raise ValidationError("criterion exit_code must be an integer")
    try:
        _canonical(criterion)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"criterion is not JSON-serializable: {exc}") from exc
    if session is not None and not isinstance(session, dict):
        raise ValidationError("session provenance must be a mapping")
    exclude_dir_names = _normalize_exclude_dirs(exclude_dirs)
    if not (isinstance(git_timeout, (int, float)) and not isinstance(git_timeout, bool) and git_timeout > 0):
        raise ValidationError("git_timeout must be a positive number")

    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(f"project is not a directory: {root}")
    run_dir = Path(cwd).expanduser().resolve() if cwd is not None else root
    if not run_dir.is_dir():
        raise ValidationError(f"check cwd is not a directory: {run_dir}")
    if timeout <= 0:
        raise ValidationError("timeout must be positive")
    if output_cap_bytes < 0:
        raise ValidationError("output_cap_bytes must not be negative")

    if record_id is None:
        record_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    else:
        record_id = str(record_id)
        if not _RECORD_ID.match(record_id):
            raise ValidationError(f"invalid record id: {record_id!r}")

    evidence = Path(evidence_dir).expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "logs").mkdir(parents=True, exist_ok=True)
    stdout_log = evidence / "logs" / f"{record_id}.stdout.log"
    stderr_log = evidence / "logs" / f"{record_id}.stderr.log"

    scope_list = _normalize_scopes(root, scopes)
    # Never let the evidence dir itself (records/logs written during the run)
    # enter the snapshot when it lives inside the project.
    exclude_list: list[str] = []
    try:
        if evidence != root and root in evidence.parents:
            exclude_list.append(evidence.relative_to(root).as_posix())
    except (OSError, ValueError):
        pass
    before = snapshot(root, scope_list, git_timeout=git_timeout,
                      exclude_dirs=exclude_dir_names, exclude_paths=exclude_list)
    try:
        observed = _execute(argv, run_dir, timeout, output_cap_bytes, stdout_log, stderr_log)
    except DadoError:
        for stray in (stdout_log, stderr_log):
            try:
                stray.unlink()
            except OSError:
                pass
        raise
    after = snapshot(root, scope_list, git_timeout=git_timeout,
                     exclude_dirs=exclude_dir_names, exclude_paths=exclude_list)
    stale = _diff_files(before["files"], after["files"])
    if stale:
        for stray in (stdout_log, stderr_log):
            try:
                stray.unlink()
            except OSError:
                pass
        _raise_conflict(
            f"code changed during check after running command "
            f"(exit {observed['exit_code']!r} after {observed['duration_seconds']}s)",
            stale,
        )

    logs = []
    for link in (stdout_log, stderr_log):
        digest_text, size = _hash_file(link)
        logs.append({
            "path": link.relative_to(evidence).as_posix(),
            "sha256": digest_text,
            "bytes": size,
        })

    criterion_met: bool | None = None
    if declared_exit is not None:
        criterion_met = observed["exit_code"] == declared_exit

    record: dict[str, Any] = {
        "kind": RECORD_KIND,
        "schema_version": RECORD_VERSION,
        "id": record_id,
        "created_at": _utc_now(),
        "project": str(root),
        "evidence_dir": str(evidence),
        "scopes": scope_list,
        "session": session if session is not None else {},
        "declared": {
            "command": argv,
            "criterion": criterion,
            "cwd": str(run_dir),
            "timeout_seconds": timeout,
            "output_cap_bytes": output_cap_bytes,
            "exclude_paths": exclude_list,
            "exclude_dirs": list(exclude_dir_names),
            "git_timeout": git_timeout,
        },
        "observed": {
            "command": argv,
            "cwd": str(run_dir),
            "duration_seconds": observed["duration_seconds"],
            "exit_code": observed["exit_code"],
            "timed_out": observed["timed_out"],
            "output_truncated": observed["output_truncated"],
            "stdout_bytes": observed["stdout_bytes"],
            "stderr_bytes": observed["stderr_bytes"],
            "logs": logs,
            "before_snapshot": {"digest": before["digest"], "files": before["files"], "git": before["git"]},
            "after_snapshot": {"digest": after["digest"]},
        },
        "result": {"criterion_met": criterion_met},
        "integrity": {},
    }
    record["integrity"] = {"record_sha256": record_integrity_hash(record)}
    path = evidence / f"{record_id}.json"
    atomic_write(path, json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return record


# ---------------------------------------------------------------------------
# record helpers + verification
# ---------------------------------------------------------------------------

def load_record(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a record JSON file; structural validity is checked by verify."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read evidence record {path}: {exc}") from exc
    return data


def record_integrity_hash(record: dict[str, Any]) -> str:
    """Digest of everything in the record except its own ``integrity`` block."""
    body = dict(record)
    body.pop("integrity", None)
    try:
        return _sha_hex(_canonical(body))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"record is not JSON-serializable: {exc}") from exc


def _validate_record_shape(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValidationError("evidence record must be a mapping")
    if record.get("kind") != RECORD_KIND:
        raise ValidationError(f"unexpected evidence record kind: {record.get('kind')!r}")
    if record.get("schema_version") != RECORD_VERSION:
        raise ValidationError("unsupported evidence record schema_version")
    for key, typ in (("id", str), ("created_at", str), ("project", str), ("evidence_dir", str)):
        if not isinstance(record.get(key), typ):
            raise ValidationError(f"evidence record field {key!r} missing or invalid")
    if not (isinstance(record.get("scopes"), list) and all(isinstance(s, str) for s in record["scopes"])):
        raise ValidationError("evidence record scopes must be a list of strings")
    session = record.get("session", {})
    if not isinstance(session, dict):
        raise ValidationError("evidence record session must be a mapping")
    for key in ("declared", "observed", "result"):
        if not isinstance(record.get(key), dict):
            raise ValidationError(f"evidence record {key!r} must be a mapping")
    observed = record["observed"]
    for key, types in (("command", list), ("cwd", str), ("logs", list)):
        if not isinstance(observed.get(key), types):
            raise ValidationError(f"evidence record observed.{key} missing or invalid")
    if not all(isinstance(item, str) for item in observed["command"]):
        raise ValidationError("evidence record observed.command must be argv strings")
    exit_code = observed.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise ValidationError("evidence record observed.exit_code must be an integer or null")
    before = observed.get("before_snapshot")
    if not (isinstance(before, dict) and isinstance(before.get("digest"), str)
            and isinstance(before.get("files"), dict)):
        raise ValidationError("evidence record is missing the code fingerprint")
    if not _HEX64.match(before["digest"]):
        raise ValidationError("evidence record fingerprint digest is malformed")
    for ref in observed["logs"]:
        if not (isinstance(ref, dict) and isinstance(ref.get("path"), str)
                and isinstance(ref.get("sha256"), str) and _HEX64.match(ref.get("sha256", ""))
                and isinstance(ref.get("bytes"), int)):
            raise ValidationError("evidence record log reference is malformed")
    result = record["result"]
    if result.get("criterion_met") not in (True, False, None):
        raise ValidationError("evidence record result.criterion_met must be boolean or null")
    declared = record["declared"]
    for key in ("exclude_paths", "exclude_dirs"):
        value = declared.get(key)
        if value is not None and not (isinstance(value, list)
                                      and all(isinstance(x, str) for x in value)):
            raise ValidationError(f"evidence record declared.{key} must be a list of strings")
    git_timeout = declared.get("git_timeout")
    if git_timeout is not None and not (isinstance(git_timeout, (int, float))
                                        and not isinstance(git_timeout, bool) and git_timeout > 0):
        raise ValidationError("evidence record declared.git_timeout must be a positive number")
    git_mode = record["observed"].get("before_snapshot", {}).get("git") if isinstance(
        record["observed"].get("before_snapshot"), dict) else None
    if git_mode is not None and not isinstance(git_mode, dict):
        raise ValidationError("evidence record before_snapshot.git must be a mapping")
    integrity = record.get("integrity")
    if not (isinstance(integrity, dict) and isinstance(integrity.get("record_sha256"), str)
            and _HEX64.match(integrity.get("record_sha256", ""))):
        raise ValidationError("evidence record integrity block missing or malformed")


def verify_record(
    record: dict[str, Any],
    *,
    project: str | os.PathLike[str] | None = None,
    check_code: bool = True,
) -> dict[str, Any]:
    """Verify a record; raise on any tampering, changed code (or invalid structure).

    With ``check_code=False`` only the record integrity and log hashes are
    verified (an offline check that does not look at the code tree).

    The snapshot options persisted at capture time (``exclude_dirs``,
    ``exclude_paths``, ``git_timeout``) are replayed exactly, so an unchanged
    code tree — including changes confined to excluded directories — verifies
    instead of being falsely rejected as stale. If the git backend itself
    changed (git-aware capture versus a fallback walk at verification, or the
    reverse), that is diagnosed explicitly and no stronger "code changed"
    claim is made: a fallback walk cannot distinguish deleted tracked files,
    so its fingerprints are not comparable to a git-aware capture.
    """
    _validate_record_shape(record)
    expected_hash = record_integrity_hash(record)
    if not hmac.compare_digest(expected_hash, record["integrity"]["record_sha256"]):
        raise ConflictError("evidence record violated its own integrity hash; record was tampered with")
    checked = ["record_integrity"]

    evidence = Path(record["evidence_dir"])
    for ref in record["observed"]["logs"]:
        try:
            log = (evidence / ref["path"]).resolve()
        except OSError as exc:
            raise ValidationError(f"cannot resolve evidence log path {ref['path']!r}: {exc}") from exc
        if evidence.resolve() not in log.parents and log.parent != evidence.resolve():
            raise ValidationError("evidence log reference escapes the evidence directory")
        if not log.is_file():
            raise ConflictError(f"evidence log missing: {ref['path']}")
        digest_text, size = _hash_file(log)
        if size != ref["bytes"] or digest_text != ref["sha256"]:
            raise ConflictError(f"evidence log tampered with: {ref['path']}")
    checked.append("logs")

    report: dict[str, Any] = {
        "status": "pass",
        "record_id": record["id"],
        "criterion_met": record["result"]["criterion_met"],
        "checked": checked,
    }
    if check_code:
        code_root = Path(project).expanduser().resolve() if project is not None else Path(record["project"])
        declared = record["declared"]
        current = snapshot(
            code_root,
            record["scopes"],
            git_timeout=declared.get("git_timeout") or DEFAULT_GIT_TIMEOUT,
            exclude_dirs=tuple(declared.get("exclude_dirs") or ()),
            exclude_paths=declared.get("exclude_paths") or (),
        )
        before = record["observed"]["before_snapshot"]
        # Diagnose a backend mode flip before any code-diff claim: fingerprints
        # from different backends are not comparable (a fallback walk cannot
        # tell deleted tracked files apart from absent ones).
        before_git = before.get("git")
        if isinstance(before_git, dict) \
                and before_git.get("available") != current["git"].get("available"):
            was = "git-aware" if before_git.get("available") else "files-only fallback"
            now = "git-aware" if current["git"].get("available") else "files-only fallback"
            raise ConflictError(
                f"git backend changed since evidence capture: recorded {was}, now {now}; "
                f"fingerprints across backends are not comparable — rerun the check"
            )
        stale = _diff_files(before["files"], current["files"])
        if current["digest"] != before["digest"]:
            _raise_conflict("code changed since evidence capture", stale)
        checked.append("code")
        report["project"] = str(code_root)
    report["checked"] = checked
    return report
