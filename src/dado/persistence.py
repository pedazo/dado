"""Durable persistence facade for the DADO Store.

Every multi-file state change of :class:`dado.store.Store` goes through this
module, which wraps the generic ``FileTransaction`` journal
(dado.transactions): a batch of writes/moves is journaled *before* any
destination is touched, then applied so the batch is either fully done or
(identically) replayable. Recovery is roll-forward: a journal interrupted
mid-apply is replayed verbatim; a destination that diverged from both the
captured before- and after-state is never overwritten.

Store-facing API
----------------
``commit(root, label, writes, moves)``
    Declare + seal + apply one batch under ``root/.txn`` (creator applies,
    which auto-seals). Zero-op batches skip the journal entirely.
``inventory(root)``
    Read-only classification of unsealed journals: pending (sealed, fully
    declared and recoverable), building (interrupted declaration — visible
    but intentionally unreplayable) and unreadable. Committed journals are
    part of the audit trail and are not reported.
``replay(root, trusted_roots)``
    Recovery for callers that own the state lock: applies every pending
    journal idempotently, trusting ONLY ``trusted_roots``. Explicit trusted
    roots are required — the journal's recorded roots field is validated
    against them and never used as authority (forged journals are refused,
    reported, never applied).

Properties and limitations (documented, not claimed away)
---------------------------------------------------------
* Applications are exactly ``write``/``move`` of files. Deletions and
  directory moves are outside the journal; a directory target is left
  behind as an empty staged dir by per-file archive moves on purpose (no
  data deletion, "preserve staged/empty dirs safely").
* ``events.jsonl`` business records are appended by Store only AFTER a
  commit returns, so a crash before the commit loses the event, not the
  state: replay restores the files but does not re-emit the event.
* Only the last batch of one API call can realistically be pending after a
  crash: batches run sequentially while the caller holds the state lock.
* Journals that failed to apply stay pending with "conflict" status
  inventory; nothing here deletes, seals or rewinds them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import ConflictError, ValidationError
from .transactions import BUILDING, COMMITTED, PENDING, TXN_DIRNAME, FileTransaction

# Replay outcomes used by Store.repair.
REPLAYED = "replayed"      # journal applied missing operations on disk
VERIFIED = "verified"      # idempotent: after-state already present, 0 applied
CONFLICT = "conflict"      # divergent destination; journal left pending, untouched
UNTRUSTED = "untrusted"    # journal declares roots outside the trusted set
IGNORED_BUILDING = "ignored-building"  # pre-seal declaration, recovery impossible by design
UNREADABLE = "unreadable"  # unparseable / malformed journal, never guessed at

_Op = dict[str, Any]


def commit(root: Path, label: str = "", writes: Sequence[tuple[Path, str]] = (),
           moves: Sequence[tuple[Path, Path]] = (), inject: Callable[[FileTransaction, int], None] | None = None,
           roots: Sequence[Path] | None = None) -> dict[str, Any]:
    """Journal and apply one batch of writes/moves as a single transaction.

    ``root`` is the journal anchoring directory (``root/.txn/<id>.json``);
    ``roots`` declares the writable containment, defaulting to ``[root]``.
    Returns the ``FileTransaction.apply`` result, or
    ``{"id": None, "phase": "committed", "applied": 0, "skipped": True}``
    for an empty batch (no journal is created for nothing-to-do calls).
    ``inject`` is the foundation's per-operation test hook: raising inside
    it simulates a crash that leaves a sealed, pending journal on disk.
    """
    if not writes and not moves:
        return {"id": None, "phase": COMMITTED, "applied": 0, "skipped": True}
    tx = FileTransaction.begin(root, roots=roots, label=label)
    # Moves execute before writes: for archive-style batches the index (a
    # write) must be the LAST op so a crash replays moves first, index last.
    for src, dst in moves:
        tx.move(src, dst)
    for path, content in writes:
        tx.write(path, content)
    return tx.apply(inject=inject)


def _entry(journal_path: Path, phase: str, raw: dict[str, Any] | None = None,
           error: str | None = None) -> dict[str, Any]:
    ops: list[_Op] = []
    if isinstance(raw, dict) and isinstance(raw.get("ops"), list):
        ops = raw["ops"]
    paths = sorted({str(op["dst"]) for op in ops
                    if isinstance(op, dict) and isinstance(op.get("dst"), str)})
    entry: dict[str, Any] = {
        "journal": str(journal_path),
        "id": raw.get("id") if isinstance(raw, dict) else None,
        "phase": phase,
        "label": str(raw.get("label", "")) if isinstance(raw, dict) else "",
        "ops": len(ops),
        "paths": paths,
    }
    if isinstance(raw, dict) and isinstance(raw.get("roots"), list):
        entry["roots"] = list(raw["roots"])
    if error is not None:
        entry["error"] = error
    return entry


def inventory(root: Path) -> list[dict[str, Any]]:
    """Read-only listing of unsealed journals under ``root/.txn``.

    Committed journals are skipped (audit trail); building journals are
    reported but are by-design unreplayable (an interrupted declaration can
    never be completed by recovery); unparseable journals are reported with
    an error so nothing guessed at is ever hidden.
    """
    txn_dir = Path(root) / TXN_DIRNAME
    if not txn_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for journal_path in sorted(txn_dir.glob("*.json")):
        try:
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            entries.append(_entry(journal_path, UNREADABLE, None, str(exc)))
            continue
        if not isinstance(raw, dict):
            entries.append(_entry(journal_path, UNREADABLE, None, "journal root is not an object"))
            continue
        phase = raw.get("phase")
        if phase == COMMITTED:
            continue
        if phase not in (PENDING, BUILDING):
            entries.append(_entry(journal_path, UNREADABLE, raw, f"unknown journal phase {phase!r})"))
            continue
        entries.append(_entry(journal_path, phase, raw))
    return entries


def replay(root: Path, trusted_roots: Sequence[Path]) -> list[dict[str, Any]]:
    """Recover journaled work after a crash.

    Applies every pending journal idempotently. ``trusted_roots`` is
    required and explicit: a journal whose recorded roots escape it gets
    status ``untrusted`` and is never applied. Destinations diverging from
    the journal's expectations give ``conflict`` (never overwritten, journal
    stays pending). Building and unreadable journals are reported but never
    replayed — recovery does not guess. Committed journals verify silently.
    """
    if not trusted_roots:
        raise ValueError("replay requires explicit trusted_roots")
    trusted = [Path(r).resolve() for r in trusted_roots]
    results: list[dict[str, Any]] = []
    for entry in inventory(root):
        if entry["phase"] == BUILDING:
            entry["status"] = IGNORED_BUILDING
            results.append(entry)
            continue
        if entry["phase"] != PENDING:
            entry["status"] = UNREADABLE
            results.append(entry)
            continue
        recorded = entry.get("roots")
        if not recorded or not _roots_within_trusted(recorded, trusted):
            entry["status"] = UNTRUSTED
            entry.setdefault("error", "journal declares roots outside the trusted set")
            results.append(entry)
            continue
        try:
            tx = FileTransaction.from_journal(Path(entry["journal"]), roots=trusted)
            result = tx.apply()
        except ConflictError as exc:
            # Divergent user work: never overwritten, journal stays pending.
            entry["status"] = CONFLICT
            entry["error"] = str(exc)
            results.append(entry)
            continue
        except ValidationError as exc:
            # Malformed ops or containment refusals: never guessed at,
            # reported so one broken journal cannot block the rest.
            entry["status"] = UNREADABLE
            entry["error"] = str(exc)
            results.append(entry)
            continue
        entry["status"] = REPLAYED if result.get("applied") else VERIFIED
        entry["applied"] = result.get("applied", 0)
        results.append(entry)
    return results


def _roots_within_trusted(recorded: list[Any], trusted: Sequence[Path]) -> bool:
    for candidate in recorded:
        if not isinstance(candidate, str) or not candidate:
            return False
        resolved = Path(str(candidate)).resolve()
        if not any(resolved == root or resolved.is_relative_to(root) for root in trusted):
            return False
    return True
