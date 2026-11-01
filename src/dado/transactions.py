"""Generic durable local journal for multi-file write/move transactions.

Overview
--------
A :class:`FileTransaction` bundles a set of file operations ("write" a
string to a path, "move" a file to a new path), durably journals them
*before* touching the destinations, and then applies them so that the whole
bundle is detectably complete (idempotent apply) or detectably conflicting
(a destination was edited by someone else after the capture). The journal is
plain JSON on disk with three phases:

* ``building`` — declaration in progress. The op list may be partial (a
  crash during declaration looks identical to an unfinished one), so a
  building journal is NEVER recoverable by replay and is never yielded by
  :meth:`FileTransaction.pending`. Its one acceptable fate is
  :meth:`FileTransaction.seal` (by the creator) or
  :meth:`FileTransaction.discard` (inspect, then delete).
* ``pending`` — the creator sealed the journal: the op list is final and
  immutable. This is the crash-recovery phase: replaying a pending journal
  finishes the work if the destinations are unchanged and refuses if they
  are not.
* ``committed`` — every operation verified applied; re-applying is a no-op.

Key properties (each is promised by the API and testable):

* **Stable ID** — each transaction gets one UUID (hex) at ``begin``; the id
  names the journal file and is stable across processes, threads and
  replays. Create the transaction once and pass papers by journal path.
* **Before/after content hashes** — every operation records the SHA-256 of
  the destination before the change (``None`` meaning "did not exist") and
  after it. Nothing is applied without those checks.
* **Creator intent vs replay** — the instance that ``begin()`` returned owns
  the declaration (``write``/``move``, ``seal``) and may implicitly seal
  inside its own ``apply()`` (sealed atomically before the first operation
  executes). A replay handle obtained from a *building* journal is inert:
  it can inspect or ``discard()`` the remains, but ``seal``/``apply`` are
  refused, because no one can prove the captured list was complete.
* **Idempotent replay** — applying a journal whose destinations are already
  in the recorded after-state (because a previous attempt crashed mid-apply)
  skips the already-done work and completes the rest. Applying a committed
  journal verifies and changes nothing.
* **Conflict detection** — before executing, the current content hash of
  every destination (and move source) must equal the recorded before-hash or
  the recorded after-hash (after ⇒ already applied by a crashed attempt).
  Anything else is a foreign edit: :class:`~dado.errors.ConflictError` and
  divergent user work is **never overwritten**. Recovery is roll-forward
  only; there is no automatic rollback.
* **Trusted root containment** — the ``roots`` field inside a journal file
  is never trusted as authority: the caller (or the default, anchored at the
  journal's physical ``<root>/.txn/<id>.json`` location) supplies the
  trusted roots, and a journal whose recorded roots are not a subset of the
  trusted ones is rejected as tampered ("forged"). Every operation path must
  resolve inside a trusted root, is recorded fully resolved (declaration
  already collapses symlinks), and is re-checked at every apply: a path that
  no longer resolves to itself (symlink or mount planted where real content
  lived) aborts the apply with :class:`~dado.errors.ValidationError`.
* **No imported execution** — the journal is plain JSON data. Replay
  understands only the closed operation set {"write", "move"}; unknown
  kinds, malformed journals or non-dict records raise
  :class:`~dado.errors.ValidationError`. Nothing in a journal is ever
  imported, evaluated, unpickled or executed, so an attacker who edits a
  journal file gets at most a refusal, not code execution.

Concurrency
-----------
Applying is serialized per journal by :func:`dado.fs.file_lock`, so two
processes racing to apply the same transaction are ordered: one applies, the
other observes "committed" and returns without writing (exactly once).
*Different* transactions sharing destinations are NOT serialized here — the
caller must hold its own :func:`~dado.fs.file_lock` around overlapping work
(or use disjoint roots per concurrent transaction).

Failure injection
-----------------
``apply(inject=...)`` accepts a test hook called after each executed
operation with ``(transaction, op_index)``; raising inside it simulates a
crash between writes/moves, leaving a pending journal on disk and partial
work behind — exactly the state replay is built to recover from.

Text only: :meth:`FileTransaction.write` stores content as UTF-8 ``str`` in
the journal so replay is self-sufficient. Moves are same-filesystem
(``os.replace``) renames of regular files; directories are rejected.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .errors import ConflictError, ValidationError
from .fs import atomic_write, file_lock

BUILDING = "building"
PENDING = "pending"
COMMITTED = "committed"
KNOWN_OPS = ("write", "move")
TXN_DIRNAME = ".txn"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str | None:
    """SHA-256 hex digest of a file's content, or ``None`` if it does not exist."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def _validated_journal(journal_path: Path) -> dict[str, Any]:
    """Load a journal file, rejecting malformed or foreign content safely."""
    try:
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Transaction journal not found: {journal_path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ValidationError(f"Transaction journal is not valid JSON: {journal_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"Transaction journal must be an object: {journal_path}")
    if not isinstance(raw.get("ops"), list):
        raise ValidationError(f"Transaction journal has no ops list: {journal_path}")
    if not isinstance(raw.get("roots"), list):
        raise ValidationError(f"Transaction journal has no roots list: {journal_path}")
    if raw.get("phase") not in (BUILDING, PENDING, COMMITTED):
        raise ValidationError(f"Unknown transaction phase {raw.get('phase')!r}: {journal_path}")
    return raw


def _trusted_roots(journal_path: Path, roots: Sequence[Path] | None) -> list[Path]:
    """Resolve the trusted root set for a journal the caller did not create.

    ``roots`` given by the caller always wins. The default is anchored to the
    journal's physical location: it must sit in ``<root>/.txn/<file>``, and
    the trusted root is ``<root>`` — the on-disk ``roots`` field is checked
    against this set, never used as authority.
    """
    if roots is not None:
        trusted = [Path(r).resolve() for r in roots]
        if not trusted:
            raise ValidationError("Explicit transaction roots must not be empty")
        return trusted
    if journal_path.parent.name != TXN_DIRNAME:
        raise ValidationError(
            f"Journal is not inside a {TXN_DIRNAME}/ directory: {journal_path} — "
            "its default trusted root cannot be anchored; pass trusted roots explicitly"
        )
    return [journal_path.parent.parent.resolve()]


def _validate_recorded_roots(recorded: list[Any], trusted: Sequence[Path], journal_path: Path) -> None:
    """The journal's roots field is advisory data, checked against trusted roots."""
    if not recorded:
        raise ValidationError(f"Transaction journal records no roots: {journal_path}")
    for entry in recorded:
        if not isinstance(entry, str):
            raise ValidationError(f"Transaction journal roots must be strings: {journal_path}")
        resolved = Path(entry)
        if not resolved.is_absolute() or not any(
            resolved == root or resolved.is_relative_to(root) for root in trusted
        ):
            raise ValidationError(
                f"Transaction journal records a root outside the trusted set "
                f"{[str(r) for r in trusted]} (possible forged journal): {entry}"
            )


def _assert_contained(op: dict[str, Any], roots: Sequence[Path], describe: str) -> None:
    directions = [op["dst"]] if op["op"] == "write" else [op["src"], op["dst"]]
    for direction in directions:
        target = Path(direction)
        if not target.is_absolute() or not any(_contained(target, root) for root in roots):
            raise ValidationError(
                f"Journal {describe} records operation {op['op']} outside its trusted roots: {direction}"
            )


def _assert_paths_resolve_stable(ops: Sequence[dict[str, Any]]) -> None:
    """Symlink-escape guard: recorded paths must still resolve to themselves."""
    for op in ops:
        directions = [op["dst"]] if op["op"] == "write" else [op["src"], op["dst"]]
        for direction in directions:
            recorded = Path(direction)
            current = recorded.resolve()
            if current != recorded:
                raise ValidationError(
                    f"Recorded transaction path no longer resolves to itself: {direction} "
                    f"→ {current} (symlink substitution or mount change; refusing to apply)"
                )


def _validate_op(op: Any) -> None:
    if not isinstance(op, dict):
        raise ValidationError("Journal operation records must be objects")
    kind = op.get("op")
    if kind not in KNOWN_OPS:
        raise ValidationError(
            f"Unknown journal operation kind {kind!r}; replay understands only {list(KNOWN_OPS)} "
            "(journal content is data, never executed or imported)"
        )
    for field in ("dst", "before", "after"):
        if field not in op:
            raise ValidationError(f"Journal operation missing field {field!r}")
    if not isinstance(op["dst"], str):
        raise ValidationError("Journal operation dst must be a string path")
    for field in ("before", "after"):
        value = op[field]
        if value is None:
            continue
        if not isinstance(value, str) or len(value) != 64 or not _is_hex(value):
            raise ValidationError(f"Journal {field}-hash must be a 64-char hex string or null")
    if kind == "write" and not isinstance(op.get("content"), str):
        raise ValidationError("Journal write operation requires string content")
    if kind == "move" and not isinstance(op.get("src"), str):
        raise ValidationError("Journal move operation requires a string src path")


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _contained(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


class FileTransaction:
    """Durable journal of write/move operations over trusted declared roots.

    Build with :meth:`begin` (fresh, creator handle) or
    :meth:`from_journal` (trusted replay handle). Lifecycle:
    ``building`` → ``seal()`` → ``pending`` → ``apply()`` → ``committed``.
    See the module docstring for the durability, conflict and security model.
    """

    def __init__(
        self,
        tx_id: str,
        root: Path,
        roots: Sequence[Path],
        journal_path: Path,
        phase: str,
        label: str,
        *,
        creator: bool = False,
    ) -> None:
        self.id = tx_id
        self.root = root
        self.journal_path = journal_path
        self.phase = phase
        self.label = label
        # Trusted roots always carried by the instance, never re-read from the
        # journal's roots field during apply.
        self.roots = [Path(r) for r in roots]
        self._ops: list[dict[str, Any]] = []
        self._creator = creator

    # ---- construction -------------------------------------------------

    @classmethod
    def begin(cls, root: Path, roots: Sequence[Path] | None = None, *, label: str = "") -> "FileTransaction":
        """Start a new transaction (creator handle).

        ``root`` is where journals live (``root/.txn/<id>.json``); it must
        already exist or be creatable. ``roots`` lists every directory the
        transaction may ever write to or move within; it defaults to
        ``[root]``. The journal starts in the ``building`` phase: the
        declaration is durable but the operation list is not yet complete,
        so it is invisible to recovery until :meth:`seal` (or an ``apply()``
        from this creator instance, which seals first).
        """
        root = Path(root).resolve()
        allowed = [Path(r).resolve() for r in (roots if roots is not None else [root])]
        for directory in allowed:
            directory.mkdir(parents=True, exist_ok=True)
        tx_id = uuid.uuid4().hex
        journal_path = root / TXN_DIRNAME / f"{tx_id}.json"
        tx = cls(tx_id, root, allowed, journal_path, BUILDING, label, creator=True)
        tx._dump()  # journals are created by their single exclusive owner
        return tx

    @classmethod
    def from_journal(cls, journal_path: Path, roots: Sequence[Path] | None = None) -> "FileTransaction":
        """Open a trusted replay handle onto an existing journal.

        ``roots`` supplies the explicitly trusted root set; when omitted it
        defaults to the root physically anchoring the journal
        (``journal_path.parent.parent``, requiring the ``.txn`` directory
        name). The journal's own ``roots`` field is validated as a subset of
        the trusted set and never granted authority. A building (possibly
        partially-declared) journal opens only for inspection: seal/apply on
        it are refused, recovery is impossible by design.
        """
        journal_path = Path(journal_path).resolve()
        raw = _validated_journal(journal_path)
        trusted = _trusted_roots(journal_path, roots)
        _validate_recorded_roots(raw["roots"], trusted, journal_path)
        for op in raw["ops"]:
            _validate_op(op)
            _assert_contained(op, trusted, describe=str(journal_path))
        root = journal_path.parent.parent
        return cls(
            str(raw["id"]), root, trusted, journal_path, raw["phase"], str(raw.get("label", ""))
        )._load_snapshot(raw)

    @classmethod
    def pending(cls, root: Path, roots: Sequence[Path] | None = None) -> Iterator["FileTransaction"]:
        """Yield every still-``pending`` transaction journaled under ``root``.

        Use after a crash to discover unapplied work. Declaring (``building``)
        and finished (``committed``) journals are skipped: an interrupted
        declaration can never be completed by replay. ``roots`` passes the
        explicit trusted root set to every produced handle; without it, the
        trusted roots are the physical ``root`` anchoring the journals.
        Malformed or tampered journals raise ``ValidationError`` — replay
        never guesses.
        """
        txn_dir = Path(root).resolve() / TXN_DIRNAME
        trusted = [Path(r).resolve() for r in roots] if roots is not None else [Path(root).resolve()]
        for journal_path in sorted(txn_dir.glob("*.json")):
            raw = _validated_journal(journal_path)
            if raw["phase"] == PENDING:
                yield FileTransaction.from_journal(journal_path, roots=trusted)

    def _load_snapshot(self, raw: dict[str, Any]) -> "FileTransaction":
        self._ops = [dict(op) for op in raw["ops"]]
        self.phase = raw["phase"]
        return self

    # ---- introspection -------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Replayable snapshot: id, phase, label, declared roots and captured ops."""
        return {
            "id": self.id,
            "phase": self.phase,
            "label": self.label,
            "roots": [str(r) for r in self.roots],
            "ops": [dict(op) for op in self._ops],
        }

    @property
    def ops(self) -> list[dict[str, Any]]:
        """Currently declared operations (complete only once sealed)."""
        return [dict(op) for op in self._ops]

    # ---- declaration ----------------------------------------------------

    def write(self, path: Path, content: str) -> str:
        """Declare a durable write of ``content`` (UTF-8 text) to ``path``.

        Captures the before-hash of the destination right now and snapshots
        the content into the journal so replay needs no external state.
        Only the creator handle in the building phase may declare
        operations; the resolved path must live inside the trusted roots.
        Returns the absolute path as recorded.
        """
        self._assert_declaring()
        dst = self._inside(path)
        if not isinstance(content, str):
            raise ValidationError(f"write content must be JSON-serializable UTF-8 str, got {type(content).__name__}")
        self._ops.append({
            "op": "write",
            "dst": str(dst),
            "before": file_hash(dst),
            "after": content_hash(content.encode("utf-8")),
            "content": content,
        })
        self._dump()  # durable: the journal records every op before anything is applied
        return str(dst)

    def move(self, src: Path, dst: Path) -> None:
        """Declare moving an existing file ``src`` onto ``dst`` within the roots."""
        self._assert_declaring()
        src_r = self._inside(src)
        dst_r = self._inside(dst)
        if src_r == dst_r:
            raise ValidationError(f"move source and destination are the same file: {src_r}")
        if not src_r.is_file():
            raise ValidationError(f"move source is not an existing file: {src_r}")
        self._ops.append({
            "op": "move",
            "src": str(src_r),
            "dst": str(dst_r),
            "before": file_hash(dst_r),
            "after": file_hash(src_r),
        })
        self._dump()

    def _assert_declaring(self) -> None:
        if not self._creator:
            raise ValidationError(
                f"Transaction {self.id} is a replay handle: declaring operations is reserved "
                "for the creator instance returned by begin()"
            )
        if self.phase != BUILDING:
            raise ValidationError(
                f"Transaction {self.id} is {self.phase}: the operation list is sealed and immutable"
            )

    def _inside(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        for directory in self.roots:
            if _contained(resolved, directory):
                return resolved
        raise ValidationError(f"Path {resolved} is outside the trusted transaction roots {self.roots}")

    # ---- lifecycle: seal / discard ---------------------------------------

    def _dump(self) -> None:
        """Write the journal atomically. Callers hold the journal file lock
        (or are the single exclusive owner at :meth:`begin`)."""
        self._dump_doc(self.summary())

    def _dump_doc(self, document: dict[str, Any]) -> None:
        atomic_write(self.journal_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    def seal(self) -> dict[str, Any]:
        """Make the declared op list final and recoverable: building → pending.

        The journal is flipped atomically under the journal lock. Idempotent:
        sealing an already-pending or committed transaction is a no-op. Only
        the creator handle may seal; replay handles refuse so that no one can
        turn an interrupted, possibly-partial declaration into recoverable
        work. After sealing, ``write``/``move`` are permanently refused.
        """
        if not self._creator:
            raise ValidationError(
                f"Transaction {self.id} is a replay handle: sealing is reserved for the creator "
                "instance returned by begin()"
            )
        with file_lock(self.journal_path):
            raw = _validated_journal(self.journal_path)
            if raw["phase"] == BUILDING:
                if len(raw["ops"]) != len(self._ops):
                    raise ValidationError(
                        f"Transaction {self.id}: committed declaration drifted from the journal "
                        f"(journal holds {len(raw['ops'])} ops, handle declared {len(self._ops)})"
                    )
                self.phase = PENDING
                self._dump()
            else:
                self._load_snapshot(raw)
        return {"id": self.id, "phase": self.phase}

    def prepare(self) -> dict[str, Any]:
        """Alias of :meth:`seal` for callers that name the act 'prepare'."""
        return self.seal()

    def discard(self) -> None:
        """Abandon a building transaction: delete its journal.

        Valid only while the on-disk journal is still in the building phase —
        by the creator handle or, after the creator died, by any replay
        handle inspecting the remains. Pending (sealed) and committed
        journals can never be discarded here: recovery owns them.
        """
        with file_lock(self.journal_path):
            raw = _validated_journal(self.journal_path)
            if raw["phase"] != BUILDING:
                raise ValidationError(
                    f"Transaction {raw['id']} is {raw['phase']}: only building transactions may "
                    "be discarded; sealed journals belong to recovery"
                )
            self.journal_path.unlink()
        self._ops = []

    # ---- apply ------------------------------------------------------------

    def apply(self, *, inject: Callable[["FileTransaction", int], None] | None = None) -> dict[str, Any]:
        """Apply every declared operation; idempotent under crash or replay.

        With the journal lock held (serializing concurrent processes), this:

        1. re-reads the journal from disk (the authoritative state);
        2. if it is still ``building``, the creator handle first seals it
           atomically (a building journal seen by anything else — e.g. a
           replay handle — is refused: complete-invisible to recovery);
        3. if committed, verifies the after-state and returns without writes;
        4. if pending, re-asserts trusted-root containment and symlink
           stability for every recorded path, then verifies each op
           (current hash ∈ {before, after}) — raising
           :class:`~dado.errors.ConflictError` on any divergent destination,
           which is never overwritten and leaves the journal pending;
        5. executes still-unapplied ops (write via :func:`dado.fs.atomic_write`,
           move via ``os.replace`` — already-moved ops are skipped), calling
           ``inject(tx, op_index)`` after each executed op so tests can raise
           mid-transaction;
        6. re-verifies all after-hashes, then flips the journal to
           ``committed`` durably. Only then is the bundle "done".

        Returns ``{"id", "phase": "committed", "applied": <count>}``.
        """
        with file_lock(self.journal_path):
            raw = _validated_journal(self.journal_path)
            if raw["phase"] == BUILDING:
                if not self._creator:
                    raise ValidationError(
                        f"Transaction {raw['id']} is still building (declaration interrupted): "
                        "recovery cannot seal a possibly-partial operation list. Inspect or "
                        "discard() the journal instead."
                    )
                # Creator apply seals atomically before the first execution.
                self.phase = PENDING
                raw = dict(raw, phase=PENDING)
                self._dump_doc(raw)
            self._load_snapshot(raw)
            for op in self._ops:
                _validate_op(op)
                _assert_contained(op, self.roots, describe=str(self.journal_path))
            _assert_paths_resolve_stable(self._ops)
            if self.phase == COMMITTED:
                self._verify_after()
                return {"id": self.id, "phase": COMMITTED, "applied": 0}
            self._verify_conflicts()
            applied = self._execute(inject)
            self._verify_after()
            self.phase = COMMITTED
            self._dump()
            return {"id": self.id, "phase": COMMITTED, "applied": applied}

    # ---- apply internals ---------------------------------------------------

    def _verify_conflicts(self) -> None:
        for op in self._ops:
            dst = Path(op["dst"])
            ops_description = f"Transaction {self.id} operation {op['op']} {op['dst']}"
            current = file_hash(dst)
            if current != op["before"] and current != op["after"]:
                # On-disk content diverged from both the captured before-state
                # and our own after-state → someone changed it since the capture.
                raise ConflictError(
                    f"{ops_description}: destination changed since the transaction began "
                    "(divergent edit detected; refusing to overwrite). "
                    f"expected before-hash {op['before']} or applied hash {op['after']}, found {current}"
                )
            if op["op"] == "move":
                src = Path(op["src"])
                src_hash = file_hash(src)
                if src_hash is not None and src_hash != op["after"]:
                    raise ConflictError(
                        f"{ops_description}: source {src} changed since the transaction began"
                    )
                if src_hash is None and current != op["after"]:
                    raise ConflictError(
                        f"{ops_description}: source {src} disappeared before the move (expected it applied)"
                    )

    def _execute(self, inject: Callable[["FileTransaction", int], None] | None) -> int:
        applied = 0
        for index, op in enumerate(self._ops):
            dst = Path(op["dst"])
            if file_hash(dst) == op["after"]:
                continue  # already applied by a crashed earlier attempt
            if op["op"] == "write":
                atomic_write(dst, op["content"])
            elif op["op"] == "move":
                src = Path(op["src"])
                if src.exists():  # else: the crashed attempt already moved it
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
            applied += 1
            if inject is not None:
                inject(self, index)
        return applied

    def _verify_after(self) -> None:
        for op in self._ops:
            current = file_hash(Path(op["dst"]))
            if current != op["after"]:
                raise ConflictError(
                    f"Transaction {self.id}: after-state mismatch at {op['dst']} "
                    f"(expected {op['after']}, found {current})"
                )


def pending_transactions(root: Path, roots: Sequence[Path] | None = None) -> Iterator[FileTransaction]:
    """Module-level alias of :meth:`FileTransaction.pending` for recovery flows."""
    return FileTransaction.pending(root, roots=roots)
