"""Tests for the generic durable file-transaction journal.

Covers the contract promised by ``dado.transactions``:

* stable transaction ID + journal persisted before anything is applied,
* before/after content hashes captured at declaration time,
* idempotent apply and idempotent replay after a mid-apply crash,
* conflict detection when a user edits a destination: divergent newer edits
  are never overwritten and the journal stays pending,
* operations outside the declared roots are rejected — at declaration and
  again at replay if a journal somehow records them,
* journals are inert data: unknown operation kinds never execute anything,
* concurrent apply from separate processes: exactly-once semantics under
  the journal file lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dado.errors import ConflictError, ValidationError
from dado.transactions import FileTransaction, pending_transactions

SRC = Path(__file__).resolve().parents[1] / "src"


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fail_now(tx: FileTransaction, index: int) -> None:
    raise RuntimeError(f"simulated crash after operation {index}")


def test_begin_records_stable_id_building_journal_with_hashes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "docs").mkdir(parents=True)
    old = root / "docs" / "f.md"
    old.write_text("OLD")
    tx = FileTransaction.begin(root, roots=[root], label="demo")
    assert len(tx.id) == 32 and tx.phase == "building"
    assert tx.journal_path == root / ".txn" / f"{tx.id}.json"
    tx.write(old, "NEW")
    fresh = root / "fresh" / "file.txt"
    fresh.parent.mkdir(parents=True, exist_ok=True)  # still inside the declared root
    tx.write(fresh, "CREATED")
    ops = tx.ops
    assert ops[0]["before"] == sha_of("OLD") and ops[0]["after"] == sha_of("NEW")
    assert ops[1]["before"] is None and ops[1]["after"] == sha_of("CREATED")
    on_disk = json.loads(tx.journal_path.read_text(encoding="utf-8"))
    assert on_disk["phase"] == "building" and len(on_disk["ops"]) == 2
    assert on_disk["roots"] == [str(root.resolve())]


def test_seal_marks_the_complete_list_pending_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    tx = FileTransaction.begin(root)
    tx.write(root / "a.txt", "A")
    assert json.loads(tx.journal_path.read_text())["phase"] == "building"
    assert not list(pending_transactions(root))  # building is invisible to recovery
    sealed = tx.seal()
    assert sealed["phase"] == "pending"
    assert json.loads(tx.journal_path.read_text())["phase"] == "pending"
    assert [t.id for t in pending_transactions(root)] == [tx.id]
    with pytest.raises(ValidationError):
        tx.write(root / "b.txt", "B")  # sealed list is immutable
    # seal is idempotent
    assert tx.seal()["phase"] == "pending"
    assert tx.prepare()["phase"] == "pending"


def test_interrupted_declaration_is_never_recoverable(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    # "Crash" during begin: journal exists, building, no ops at all.
    orphan = FileTransaction.begin(root)
    orphan_journal = orphan.journal_path
    raw = json.loads(orphan_journal.read_text())
    assert raw["phase"] == "building" and raw["ops"] == []
    # Crash after op 1 of 2: the on-disk list is provably not the creator's intent
    # (the remaining ops were never captured).
    partial = FileTransaction.begin(root)
    partial.write(root / "one.txt", "1")
    for _tx in (orphan, partial):
        assert not list(pending_transactions(root))  # excluded from recovery
    orphan_handle = FileTransaction.from_journal(orphan_journal)
    assert orphan_handle.phase == "building"
    with pytest.raises(ValidationError) as err:
        orphan_handle.apply()
    assert "building" in str(err.value)
    partial_handle = FileTransaction.from_journal(partial.journal_path)
    with pytest.raises(ValidationError):
        FileTransaction.from_journal(partial.journal_path).apply()
    assert not (root / "one.txt").exists()  # nothing was executed from a partial list

    # The preserved remains are inspectable and discardable, but never applied:
    assert partial_handle.ops[0]["content"] == "1"
    partial_handle.discard()  # works from any handle while the journal is building
    assert not partial.journal_path.exists()
    with pytest.raises(ValidationError):
        partial_handle.discard()  # journal already gone → validation error

    # The creator's own in-flight handle seals first inside apply() and completes:
    stranger = FileTransaction.begin(root)
    stranger.write(root / "two.txt", "2")
    assert not list(pending_transactions(root))  # still building: invisible
    result = stranger.apply()  # atomically seals, then executes the complete list
    assert result == {"id": stranger.id, "phase": "committed", "applied": 1}
    assert (root / "two.txt").read_text() == "2"
    assert [t.id for t in pending_transactions(root)] == []  # committed now


def test_apply_writes_and_moves_then_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    archive = root / "archive"
    archive.mkdir(parents=True)
    target = root / "report.md"
    target.write_text("v1")
    (archive / "evidence.txt").write_text("EV")

    tx = FileTransaction.begin(root)
    tx.write(target, "v2 rewritten")
    tx.move(archive / "evidence.txt", archive / "kept.txt")

    result = tx.apply()
    assert result == {"id": tx.id, "phase": "committed", "applied": 2}
    assert target.read_text() == "v2 rewritten"
    assert not (archive / "evidence.txt").exists()
    assert (archive / "kept.txt").read_text() == "EV"
    assert json.loads(tx.journal_path.read_text())["phase"] == "committed"

    again = tx.apply()
    assert again["applied"] == 0 and again["phase"] == "committed"
    assert target.read_text() == "v2 rewritten"  # committed apply changes nothing


def test_replay_completes_after_mid_apply_crash(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    inbox = root / "inbox"
    done = root / "done"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("CONTENT")

    tx = FileTransaction.begin(root)
    partial = root / "partial.md"
    tx.write(partial, "PARTIAL")
    tx.move(inbox / "doc.md", done / "doc.md")

    # Simulated crash after the first executed operation (between writes/moves).
    with pytest.raises(RuntimeError):
        tx.apply(inject=fail_now)

    applied = tx.journal_path
    journal = json.loads(applied.read_text())
    assert journal["phase"] == "pending" and len(journal["ops"]) == 2
    assert partial.read_text() == "PARTIAL"  # first op landed before the crash

    # Recovery: replay from another handle in a "different process/machine" spirit.
    recovered = FileTransaction.from_journal(applied)
    assert recovered.id == tx.id and recovered.phase == "pending"
    result = recovered.apply()
    assert result["phase"] == "committed"
    assert not (inbox / "doc.md").exists()
    assert (done / "doc.md").read_text() == "CONTENT"
    # Re-running a committed recovery is a verified no-op.
    assert FileTransaction.from_journal(applied).apply()["applied"] == 0
    assert not list(pending_transactions(root))


def test_user_edited_destination_is_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    note = root / "note.md"
    note.write_text("ORIGINAL")
    tx = FileTransaction.begin(root)
    tx.write(note, "TX VERSION")

    # User diverged after the transaction captured its before-hash.
    # Nothing applied yet: current-on-disk is neither before nor after.
    note.write_text("USER'S NEWER EDIT")
    with pytest.raises(ConflictError) as err:
        tx.apply()
    assert "divergent" in str(err.value)
    assert note.read_text() == "USER'S NEWER EDIT"  # never overwritten
    assert json.loads(tx.journal_path.read_text())["phase"] == "pending"

    # Same rule post-crash for an op the transaction already applied:
    tx2 = FileTransaction.begin(root)
    tx2.write(note, "ROLL-FORWARD")
    with pytest.raises(RuntimeError):
        tx2.apply(inject=fail_now)
    note.write_text("USER'S DIVERGENT REWRITE")
    with pytest.raises(ConflictError):
        FileTransaction.from_journal(tx2.journal_path).apply()
    assert note.read_text() == "USER'S DIVERGENT REWRITE"

    # And for a move whose source the user edited divergently:
    moved_dir = root / "moved"
    moved_dir.mkdir()
    src = root / "src.txt"
    src.write_text("TO-MOVE")
    tx3 = FileTransaction.begin(root)
    tx3.move(src, moved_dir / "dst.txt")
    src.write_text("USER CHANGED THE SOURCE")
    with pytest.raises(ConflictError):
        tx3.apply()
    assert not (moved_dir / "dst.txt").exists()


def test_declared_ops_must_respect_roots(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    tx = FileTransaction.begin(root, roots=[root])
    with pytest.raises(ValidationError):
        tx.write(outside / "f.txt", "nope")
    with pytest.raises(ValidationError):
        tx.move(outside / "a.txt", root / "b.txt")

    # A hand-edited journal referencing outside roots is rejected at replay.
    inside = root / "inside.txt"
    inside.write_text("based")
    tx2 = FileTransaction.begin(root, roots=[root])
    tx2.write(inside, "edited")
    forged = json.loads(tx2.journal_path.read_text())
    forged["ops"][0]["dst"] = str((outside / "f.txt").resolve())
    tx2.journal_path.write_text(json.dumps(forged))
    with pytest.raises(ValidationError):
        FileTransaction.from_journal(tx2.journal_path)


def test_journal_is_inert_data_never_executed(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    tx = FileTransaction.begin(root)
    tx.write(root / "ok.txt", "fine")
    forged = json.loads(tx.journal_path.read_text())
    forged["ops"] = [{
        "op": "run",
        "dst": str(root / "ok.txt"),
        "before": None,
        "after": None,
        "import": "os.system",  # hostile content: must be refused, not interpreted
        "code": "print('pwned')",
    }]
    tx.journal_path.write_text(json.dumps(forged))
    with pytest.raises(ValidationError) as err:
        FileTransaction.from_journal(tx.journal_path)
    assert "run" in str(err.value)
    assert not (root / "ok.txt").exists()  # nothing was executed/written


def test_move_validation(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    tx = FileTransaction.begin(root)
    with pytest.raises(ValidationError):
        tx.move(root / "a" / "missing.txt", root / "b" / "x.txt")
    (root / "b" / "same.txt").write_text("S")
    with pytest.raises(ValidationError):
        tx.move(root / "b" / "same.txt", root / "b" / "same.txt")

    # Non-str content for write is refused before the journal is touched.
    with pytest.raises(ValidationError):
        tx.write(root / "bin.dat", b"bytes")  # type: ignore[arg-type]


def test_concurrent_apply_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    stage = root / "stage"
    final = root / "final"
    (stage / "p").mkdir(parents=True)
    (stage / "p" / "f.md").write_text("ONE")
    (stage / "q").mkdir()

    tx = FileTransaction.begin(root)
    tx.write(stage / "q" / "f.md", "TWO")
    tx.move(stage / "p" / "f.md", final / "f.md")
    with pytest.raises(RuntimeError):
        tx.apply(inject=fail_now)  # crash after op 0: q/f.md already carries "TWO"

    journal = str(tx.journal_path)
    worker = (
        "import os, sys\n"
        "sys.path.insert(0, os.environ['SRC'])\n"
        "from dado.transactions import FileTransaction\n"
        "r = FileTransaction.from_journal(sys.argv[1]).apply()\n"
        "print(f\"{r['phase']} {r['applied']}\")\n"
    )
    env = dict(os.environ, SRC=str(SRC))
    procs = [subprocess.Popen([sys.executable, "-c", worker, journal], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    outs = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        assert p.returncode == 0, stderr
        assert "Traceback" not in stderr  # replays racing each other are handled, never crash
        outs.append(stdout.strip())

    # Every replay reports committed; the work happened exactly once.
    assert all(line.startswith("committed") for line in outs)
    assert sum(int(line.split()[1]) for line in outs) == 1
    assert (stage / "q" / "f.md").read_text() == "TWO"
    assert (final / "f.md").read_text() == "ONE"
    assert not (stage / "p" / "f.md").exists()
    assert json.loads(tx.journal_path.read_text())["phase"] == "committed"


# ---- trusted roots / symlink-escape regressions (verifier finding 2) ------


def test_forged_roots_field_never_grants_authority(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tx = FileTransaction.begin(root)
    tx.write(root / "real.txt", "truth")

    # 1. Recorded roots mutated to include the outside directory.
    forged = json.loads(tx.journal_path.read_text())
    forged["roots"] = [str(root), str(outside)]
    tx.journal_path.write_text(json.dumps(forged))
    with pytest.raises(ValidationError) as err:
        FileTransaction.from_journal(tx.journal_path)  # default anchor: root
    assert "trusted" in str(err.value)
    # Even if a caller confusingly passes the forged set, ops outside the
    # actual trusted set stay refused:
    legit = json.loads(tx.journal_path.read_text())
    legit["ops"][0]["dst"] = str((outside / "victim.txt").resolve())
    tx.journal_path.write_text(json.dumps(legit))
    with pytest.raises(ValidationError):
        FileTransaction.from_journal(tx.journal_path, roots=[root])
    assert not (outside / "victim.txt").exists()


def test_mutated_root_field_and_op_equally_refused(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    tx = FileTransaction.begin(root)
    tx.write(root / "a.txt", "A")

    # 2. A non-directory / fake root (typo or hostile) can't pass as trusted.
    forged = json.loads(tx.journal_path.read_text())
    forged["roots"] = [str(tmp_path / "nope")]
    tx.journal_path.write_text(json.dumps(forged))
    with pytest.raises(ValidationError):
        FileTransaction.from_journal(tx.journal_path)

    # 3. Same trust rule applied through the recovery scanner.
    sealed = FileTransaction.begin(root)
    sealed.write(root / "b.txt", "B")
    sealed.seal()
    forged_scan = json.loads(sealed.journal_path.read_text())
    forged_scan["ops"][0]["dst"] = str((tmp_path / "elsewhere.bin").resolve())
    sealed.journal_path.write_text(json.dumps(forged_scan))
    with pytest.raises(ValidationError):
        list(FileTransaction.pending(root, roots=[root]))
    assert not (root / "elsewhere.bin").exists()


def test_symlink_escape_is_rejected_at_declaration_and_apply(tmp_path: Path, symlink_capable) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Declaration: writing through a symlink that escapes the roots is refused.
    escape = root / "escape"
    escape.symlink_to(outside)
    tx = FileTransaction.begin(root)
    with pytest.raises(ValidationError):
        tx.write(escape / "f.txt", "smuggled")
    assert not list(outside.iterdir())

    # Apply-time: a symlink planted under the destination parent after the
    # capture makes the recorded path resolve elsewhere → refusal, nothing
    # written outside.
    sub = root / "sub"
    sub.mkdir()
    victim_payload = "untouched"
    (outside / "hostile.txt").write_text(victim_payload)
    tx2 = FileTransaction.begin(root)
    tx2.write(sub / "f.txt", "planned")
    shutil.rmtree(sub)
    sub.symlink_to(outside)  # relocation attack between declare and apply
    with pytest.raises(ValidationError) as err:
        tx2.apply()
    assert "self" in str(err.value)
    assert (outside / "hostile.txt").read_text() == victim_payload
    # Nothing committed; the journal became sealed-pending (the creator triggered
    # the seal) and therefore left recovery's hands: discard is refused.
    assert json.loads(tx2.journal_path.read_text())["phase"] == "pending"
    with pytest.raises(ValidationError):
        tx2.discard()
