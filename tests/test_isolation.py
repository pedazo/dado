"""Focused checks for the local isolated-environment foundations.

Covers: two independent snapshots + baseline metadata, dirty/no-git support,
exclusions, unrelated-file retention, integrate scope, overlapping-conflict
all-or-nothing, deletions, binary round-trip, symlinks (inside + escape),
destination path escape rejection, interrupted integration + resume, explicit
resource reservations, and the CLI-compatible main(argv).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from dado import isolation as iso
from dado.errors import ConflictError, ValidationError


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _snapshot(tmp_path: Path, scope: list[str], **kw) -> dict:
    return iso.create_snapshot(tmp_path, scope, **kw)


def test_two_snapshots_are_independent_with_baseline_metadata(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    first = _snapshot(tmp_path, ["src"], execution_id="exec-one")
    _write(tmp_path / "src" / "a.py", "A2")
    _write(tmp_path / "new" / "untracked.txt", "u")
    second = _snapshot(tmp_path, ["src", "new"], execution_id="exec-two")

    assert first["execution_id"] == "exec-one"
    assert second["execution_id"] == "exec-two"
    assert first["files"]["src/a.py"]["hash"] != second["files"]["src/a.py"]["hash"]
    assert "new/untracked.txt" in second["files"] and "new/untracked.txt" not in first["files"]
    # Baseline hashes match the env byte content (copy integrity).
    assert first["files"]["src/a.py"]["hash"] == iso._hash_bytes(
        (Path(first["snapshot_dir"]) / "files" / "src/a.py").read_bytes()
    )
    listed = {item["execution_id"] for item in iso.list_snapshots(tmp_path)}
    assert {"exec-one", "exec-two"} <= listed


def test_snapshots_dirty_and_untracked_files_without_git(tmp_path: Path) -> None:
    # No .git anywhere: must work purely from the filesystem.
    assert not (tmp_path / ".git").exists()
    _write(tmp_path / "src" / "main.py", "print('dirty line')")
    env = _snapshot(tmp_path, ["src"])
    copied = (Path(env["snapshot_dir"]) / "files" / "src/main.py").read_text()
    assert copied == "print('dirty line')"


def test_default_exclusions_and_secret_refusal(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "ok.py", "x")
    _write(tmp_path / "src" / "__pycache__" / "m.pyc", b"\x00")
    _write(tmp_path / "src" / "lib.key", "SECRET")
    _write(tmp_path / "src" / ".env", "TOKEN=1")
    metadata = _snapshot(tmp_path, ["src"])
    assert "src/ok.py" in metadata["files"]
    assert "src/__pycache__/m.pyc" not in metadata["files"]
    assert "src/lib.key" in metadata["skipped"]
    assert "src/.env" in metadata["skipped"]
    with pytest.raises(ValidationError):
        _snapshot(tmp_path, ["src/.env"])


def test_integrate_retains_unrelated_destination_changes(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    _write(tmp_path / "docs" / "other.md", "old")
    env = _snapshot(tmp_path, ["src"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.py", "A-EDITED")
    # Destination change unrelated to the task (outside env scope).
    _write(tmp_path / "docs/other.md", "USER EDIT RETAINED")
    _write(tmp_path / "src" / "untouched.py", "A")

    result = iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert result["applied"] == ["src/a.py"]
    assert result["requires_verification"] is True
    assert (tmp_path / "src/a.py").read_text() == "A-EDITED"
    assert (tmp_path / "src/untouched.py").read_text() == "A"
    assert (tmp_path / "docs/other.md").read_text() == "USER EDIT RETAINED"


def test_integrate_scope_limits_output(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    _write(tmp_path / "tests" / "b.py", "B")
    env = _snapshot(tmp_path, ["src", "tests"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.py", "A-EDITED")
    _write(files_dir / "tests/b.py", "B-EDITED")
    result = iso.integrate_snapshot(tmp_path, env["execution_id"], scope=["src"])
    assert result["applied"] == ["src/a.py"]
    assert (tmp_path / "src/a.py").read_text() == "A-EDITED"
    assert (tmp_path / "tests/b.py").read_text() == "B"  # outside scope: retained


def test_overlapping_conflict_is_all_or_nothing(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    _write(tmp_path / "src" / "b.py", "B")
    env = _snapshot(tmp_path, ["src"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.py", "A-EDITED")
    _write(files_dir / "src/b.py", "B-EDITED")
    # Destination edits the same files since the snapshot -> overlap.
    _write(tmp_path / "src/b.py", "USER EDIT")
    with pytest.raises(ConflictError) as excinfo:
        iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert "src/b.py" in str(excinfo.value)
    assert (tmp_path / "src/a.py").read_text() == "A"  # nothing applied
    assert (tmp_path / "src/b.py").read_text() == "USER EDIT"


def test_destination_side_change_with_unchanged_source_is_retained(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"])
    _write(tmp_path / "src/a.py", "DEST EDITED")
    result = iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert result["applied"] == []
    assert (tmp_path / "src/a.py").read_text() == "DEST EDITED"


def test_deletion_and_binary_roundtrip(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "goes.txt", "delete me")
    _write(tmp_path / "src" / "blob.bin", bytes(range(256)))
    env = _snapshot(tmp_path, ["src"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    (files_dir / "src/goes.txt").unlink()
    new_blob = bytes([(i * 7) % 256 for i in range(4096)])
    _write(files_dir / "src/blob.bin", new_blob)
    result = iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert sorted(result["applied"]) == ["src/blob.bin"]
    assert result["deleted"] == ["src/goes.txt"]
    assert not (tmp_path / "src/goes.txt").exists()
    assert (tmp_path / "src/blob.bin").read_bytes() == new_blob


def test_symlinks_inside_scope_and_escape_rejected(tmp_path: Path, symlink_capable) -> None:
    _write(tmp_path / "src" / "target.py", "T")
    (tmp_path / "src" / "link.py").symlink_to(tmp_path / "src" / "target.py")
    # Link pointing outside the project root entirely.
    outside_file = tmp_path.parent / "iso-escape-target.txt"
    _write(outside_file, "X")
    (tmp_path / "src" / "escape.py").symlink_to(outside_file)
    with pytest.raises(ValidationError) as excinfo:
        _snapshot(tmp_path, ["src"])
    assert "src/escape.py" in str(excinfo.value)
    # Snapshot without the escape symlink: internal link copied as a regular
    # file; when its content changes the destination symlink is atomically
    # replaced (never written through), yielding a regular file in the project.
    (tmp_path / "src" / "escape.py").unlink()
    env = _snapshot(tmp_path, ["src"])
    link = Path(env["snapshot_dir"]) / "files" / "src/link.py"
    assert link.is_file() and not link.is_symlink()
    _write(link, "T-CHANGED")
    result = iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert result["applied"] == ["src/link.py"]
    dest_link = tmp_path / "src/link.py"
    assert dest_link.is_file() and not dest_link.is_symlink()
    assert dest_link.read_text() == "T-CHANGED"


def test_destination_path_escape_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"])
    metadata = iso.load_metadata(tmp_path, env["execution_id"])
    # Direct check of the destination-path contract.
    with pytest.raises(ValidationError):
        iso._check_dest_path(tmp_path, "../evil.txt")
    with pytest.raises(ValidationError):
        iso._check_dest_path(tmp_path, "/etc/passwd")
    # End-to-end: a corrupted (registered) manifest path escaping the project
    # bumps into the same guard before any write happens.
    payload = Path(env["snapshot_dir"]) / "out-of-project.txt"
    _write(payload, "P")  # found at files/src/../../out-of-project.txt
    rel_escape = "src/../../out-of-project.txt"
    registry_path = iso._registry_path(tmp_path)
    registry = json.loads(registry_path.read_text())
    registry["executions"][env["execution_id"]]["base_manifest"][rel_escape] = {
        "hash": "0" * 64,
        "mode": 0o644,
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert not (tmp_path.parent / "out-of-project.txt").exists()


def test_interrupted_integration_resume_and_env_preserved(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "src" / "a.txt", "A")
    _write(tmp_path / "src" / "b.txt", "B")
    env = _snapshot(tmp_path, ["src"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.txt", "A-NEW")
    _write(files_dir / "src/b.txt", "B-NEW")
    env_before = {p: p.read_bytes() for p in files_dir.rglob("*") if p.is_file()}

    real_apply = iso._apply_one
    hit = {"interrupted": False}

    def flaky(project: Path, staging_dir: Path, entry: dict) -> None:
        # Fail exactly once, like a process dying mid-integration; a later
        # (resumed) run may retry the same file.
        if entry["rel"] == "src/b.txt" and not hit["interrupted"]:
            hit["interrupted"] = True
            raise OSError("simulated interruption")
        return real_apply(project, staging_dir, entry)

    monkeypatch.setattr(iso, "_apply_one", flaky)
    with pytest.raises(iso.IntegrationInterrupted) as excinfo:
        iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert "src/b.txt" in str(excinfo.value)
    # Environment is preserved byte-for-byte; destination has partial state.
    assert (tmp_path / "src/a.txt").read_text() == "A-NEW"
    assert (tmp_path / "src/b.txt").read_text() == "B"
    assert {p: p.read_bytes() for p in files_dir.rglob("*") if p.is_file()} == env_before
    # Resume finishes the plan.
    result = iso.integrate_snapshot(tmp_path, env["execution_id"], resume=True)
    assert result["applied"] == ["src/b.txt"]
    assert (tmp_path / "src/b.txt").read_text() == "B-NEW"
    assert result["requires_verification"] is True
    assert not list((Path(env["snapshot_dir"]) / ".journals").glob("*journal.json"))


def test_incomplete_journal_blocks_until_resumed_or_discarded(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "src" / "a.txt", "A")
    env = _snapshot(tmp_path, ["src"])
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.txt", "A-NEW")
    monkeypatch.setattr(
        iso, "_apply_one", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
    )
    with pytest.raises(iso.IntegrationInterrupted):
        iso.integrate_snapshot(tmp_path, env["execution_id"])
    monkeypatch.undo()
    with pytest.raises(iso.IntegrationInterrupted):
        iso.integrate_snapshot(tmp_path, env["execution_id"])
    discarded = iso.discard_journal(tmp_path, env["execution_id"])
    assert discarded["requires_verification"] is True
    # After discard, a fresh integrate applies cleanly.
    result = iso.integrate_snapshot(tmp_path, env["execution_id"])
    assert result["applied"] == ["src/a.txt"]


def test_resource_reservations_explicit_release_only(tmp_path: Path) -> None:
    iso.reserve(tmp_path, "exec-1", ["port:4319", "db:work.sqlite"])
    with pytest.raises(ConflictError):
        iso.reserve(tmp_path, "exec-2", ["port:4319"])
    # Same execution may extend its own reservations.
    iso.reserve(tmp_path, "exec-1", ["build:dist"])
    registry = iso._load_registry(tmp_path)
    assert registry["executions"]["exec-1"]["reservations"] == [
        "build:dist",
        "db:work.sqlite",
        "port:4319",
    ]
    iso.release(tmp_path, "exec-1", ["port:4319"])
    with pytest.raises(ConflictError):  # db label still held
        iso.reserve(tmp_path, "exec-2", ["db:work.sqlite"])
    iso.release(tmp_path, "exec-1")
    assert not iso._load_registry(tmp_path)["executions"]["exec-1"]["reservations"]
    iso.reserve(tmp_path, "exec-2", ["port:4319"])


def test_main_cli_create_list_integrate(tmp_path: Path, capsys) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    snapshot_id = "cli-exec"
    created = iso.main(
        ["create", "--project", str(tmp_path), "--scope", "src", "--id", snapshot_id]
    )
    assert created == 0
    listed = iso.main(["list", "--project", str(tmp_path)])
    assert listed == 0
    plain = capsys.readouterr()
    assert snapshot_id in plain.out

    files_dir = iso.default_isolation_root(tmp_path) / snapshot_id / "files"
    _write(files_dir / "src/a.py", "A-CLI")
    code = iso.main(["integrate", "--project", str(tmp_path), "--snapshot", snapshot_id])
    assert code == 0
    assert (tmp_path / "src/a.py").read_text() == "A-CLI"

    code_err = iso.main(["integrate", "--project", str(tmp_path), "--snapshot", "nope"])
    assert code_err == 1
    err = capsys.readouterr().err
    assert "error" in json.loads(err)


def test_snapshot_rejects_dado_and_scope_escape(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    iso.create_snapshot(tmp_path, ["src"], execution_id="s1")  # creates .dado/local
    with pytest.raises(ValidationError):
        iso.create_snapshot(tmp_path, [".dado/local/isolation"], execution_id="s2")
    with pytest.raises(ValidationError):
        iso.create_snapshot(tmp_path, ["../elsewhere"], execution_id="s3")
    with pytest.raises(ValidationError):
        iso.create_snapshot(tmp_path, ["/etc"], execution_id="s4")
    with pytest.raises(ConflictError):
        iso.create_snapshot(tmp_path, ["src"], execution_id="s1")


# --------------------------------------------------------------------------
# Adversarial scenarios (verifier probes 1-7), plus boundary conditions.
# --------------------------------------------------------------------------


def test_snapshot_cannot_integrate_into_a_different_project(tmp_path: Path) -> None:
    """probe1: snapshot of A must not write into B."""
    a, b = tmp_path / "projA", tmp_path / "projB"
    _write(a / "src/task.py", "TASK WORK")
    env = iso.create_snapshot(a, ["src"], execution_id="probe-assoc")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/task.py", "TASK WORK-EDITED")
    (b / "src").mkdir(parents=True)
    with pytest.raises(ConflictError) as excinfo:
        iso.integrate_snapshot(b, str(env["snapshot_dir"]))
    assert "belongs to project" in str(excinfo.value)
    assert not (b / "src/task.py").exists()
    # By id it is simply unknown in B's registry/default root (still an error).
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(b, env["execution_id"])


def test_new_vs_new_is_a_controlled_conflict(tmp_path: Path) -> None:
    """probe2: env-added file also added at dest -> ConflictError, no KeyError."""
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"], execution_id="p2")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/new.py", "BOTH SIDES ADDED THIS")
    _write(tmp_path / "src/new.py", "USER VERSION")
    with pytest.raises(ConflictError) as excinfo:
        iso.integrate_snapshot(tmp_path, "p2")
    assert "src/new.py" in str(excinfo.value)
    assert (tmp_path / "src/new.py").read_text() == "USER VERSION"
    # Identical content on both sides => idempotent no-op, nothing rewritten.
    _write(files_dir / "src/new.py", "USER VERSION")
    result = iso.integrate_snapshot(tmp_path, "p2")
    assert result["applied"] == []


def test_tampered_snapshot_json_cannot_widen_scope_or_hijack(tmp_path: Path) -> None:
    """probe3: editing snapshot.json scope/files must have no effect."""
    _write(tmp_path / "src" / "a.py", "A")
    _write(tmp_path / "docs" / "victim.txt", "KEEP ME")
    env = _snapshot(tmp_path, ["src"], execution_id="p3")
    md_path = Path(env["snapshot_dir"]) / "snapshot.json"
    md = json.loads(md_path.read_text())
    md["scope"] = ["src", "docs"]
    md["files"]["docs/victim.txt"] = {
        "hash": iso._hash_bytes(b"KEEP ME"),
        "mode": 0o644,
    }
    md_path.write_text(json.dumps(md))
    result = iso.integrate_snapshot(tmp_path, "p3")
    assert "docs/victim.txt" not in result["deleted"]
    assert (tmp_path / "docs/victim.txt").read_text() == "KEEP ME"


def test_unregistered_snapshot_cannot_integrate(tmp_path: Path) -> None:
    """A snapshot whose registry record was destroyed must be refused."""
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"], execution_id="ghost")
    registry_path = iso._registry_path(tmp_path)
    registry = json.loads(registry_path.read_text())
    del registry["executions"]["ghost"]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ConflictError) as excinfo:
        iso.integrate_snapshot(tmp_path, "ghost")
    assert "not registered" in str(excinfo.value)


def test_integrate_scope_may_only_narrow(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    _write(tmp_path / "tests" / "b.py", "B")
    env = _snapshot(tmp_path, ["src", "tests"], execution_id="narrow")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.py", "A-EDITED")
    _write(files_dir / "tests/b.py", "B-EDITED")
    # Widening beyond the trusted snapshot scope is refused...
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(tmp_path, "narrow", scope=["src", "more"])
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(tmp_path, "narrow", scope=["../etc"])
    # ...narrowing applies exactly the subset.
    result = iso.integrate_snapshot(tmp_path, "narrow", scope=["tests"])
    assert result["applied"] == ["tests/b.py"]
    assert (tmp_path / "tests/b.py").read_text() == "B-EDITED"
    assert (tmp_path / "src/a.py").read_text() == "A"
    # Tampering snapshot.json's recorded scope cannot widen either (registry rules).
    md_path = Path(env["snapshot_dir"]) / "snapshot.json"
    md = json.loads(md_path.read_text())
    md["scope"] = ["src", "tests", "untracked-new"]
    md_path.write_text(json.dumps(md))
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(tmp_path, "narrow", scope=["untracked-new"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not supported by Windows chmod")
def test_file_modes_preserved_on_apply(tmp_path: Path) -> None:
    """probe6(2): 0755 stays executable, 0600 stays private."""
    prog = tmp_path / "src" / "run.sh"
    secret = tmp_path / "src" / "cred.conf"
    _write(prog, "#!/bin/sh\necho hi\n")
    prog.chmod(0o755)
    _write(secret, "x=1\n")
    secret.chmod(0o600)
    env = _snapshot(tmp_path, ["src"], execution_id="modes")
    assert env["files"]["src/run.sh"]["mode"] == 0o755
    assert env["files"]["src/cred.conf"]["mode"] == 0o600
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/run.sh", "#!/bin/bash\necho NEW\n")
    _write(files_dir / "src/cred.conf", "x=2\n")
    result = iso.integrate_snapshot(tmp_path, "modes")
    assert sorted(result["applied"]) == ["src/cred.conf", "src/run.sh"]
    assert stat.S_IMODE(os.stat(tmp_path / "src/run.sh").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(tmp_path / "src/cred.conf").st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not supported by Windows chmod")
def test_mode_change_counts_as_change_and_can_conflict(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "f.sh", "x")
    (tmp_path / "src" / "f.sh").chmod(0o644)
    env = _snapshot(tmp_path, ["src"], execution_id="modeconf")
    files_dir = Path(env["snapshot_dir"]) / "files"
    os.chmod(files_dir / "src/f.sh", 0o755)
    # Destination untouched by mode or content -> env mode change applies.
    result = iso.integrate_snapshot(tmp_path, "modeconf")
    assert result["applied"] == ["src/f.sh"]
    assert stat.S_IMODE(os.stat(tmp_path / "src/f.sh").st_mode) == 0o755
    # Both sides change mode differently (dest untouched by integrate) -> conflict.
    env2 = _snapshot(tmp_path, ["src"], execution_id="modeconf2")
    os.chmod(Path(env2["snapshot_dir"]) / "files" / "src/f.sh", 0o600)
    (tmp_path / "src" / "f.sh").chmod(0o444)
    with pytest.raises(ConflictError):
        iso.integrate_snapshot(tmp_path, "modeconf2")
    assert stat.S_IMODE(os.stat(tmp_path / "src" / "f.sh").st_mode) == 0o444


def test_resume_with_empty_residual_plan_finalizes_journal(
    tmp_path: Path, monkeypatch
) -> None:
    """probe6(1): full-apply crash before journal cleanup must not block."""
    _write(tmp_path / "src" / "y.txt", "Y")
    env = _snapshot(tmp_path, ["src"], execution_id="emptyresidual")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/y.txt", "Y-EDITED")
    # Simulate: everything applied, then a crash before journal cleanup.
    monkeypatch.setattr(
        iso, "_apply_one", lambda *a, **k: (_ for _ in ()).throw(OSError("crash on first step"))
    )
    JR = Path(env["snapshot_dir"]) / ".journals"
    with pytest.raises(iso.IntegrationInterrupted):
        iso.integrate_snapshot(tmp_path, "emptyresidual")
    jdir = next(JR.iterdir())
    journal = json.loads((jdir / "journal.json").read_text())
    journal["applied"] = [{"rel": "src/y.txt", "action": "write"}]
    journal["pending"] = []
    (jdir / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    monkeypatch.undo()  # real apply for the resumed run
    # probe6(1) models a journal lying about applied entries: the resumed run
    # reconciles dest to the environment and finalizes the journal.
    result = iso.integrate_snapshot(tmp_path, "emptyresidual", resume=True)
    assert result["applied"] == ["src/y.txt"]
    assert list(JR.iterdir()) == []  # journal finalized
    # A later fresh integrate is not blocked and is an idempotent no-op.
    fresh = iso.integrate_snapshot(tmp_path, "emptyresidual")
    assert fresh["applied"] == []
    assert fresh["requires_verification"] is False
    assert (tmp_path / "src/y.txt").read_text() == "Y-EDITED"


def test_resume_replays_partial_journal_then_finalizes(
    tmp_path: Path, monkeypatch
) -> None:
    """probe5(a): crash before journal cleanup on the LAST run auto-recovers."""
    _write(tmp_path / "src" / "x.txt", "X")
    env = _snapshot(tmp_path, ["src"], execution_id="replay")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/x.txt", "X-EDITED")
    JR = Path(env["snapshot_dir"]) / ".journals"
    # Phase 1: crash at the very first apply -> genuinely incomplete journal.
    monkeypatch.setattr(
        iso, "_apply_one", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    with pytest.raises(iso.IntegrationInterrupted):
        iso.integrate_snapshot(tmp_path, "replay")
    jdir = next(JR.iterdir())
    journal = json.loads((jdir / "journal.json").read_text())
    assert journal["applied"] == []
    assert [i["rel"] for i in journal["pending"]] == ["src/x.txt"]
    # A fresh (non-resume) run must refuse to act while pending work exists.
    with pytest.raises(iso.IntegrationInterrupted):
        iso.integrate_snapshot(tmp_path, "replay")
    monkeypatch.undo()
    # Phase 2: real apply succeeds, then the cleanup itself crashes (journal
    # fully accounted for, but the directory is left behind).
    monkeypatch.setattr(
        shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("crash before cleanup"))
    )
    result = iso.integrate_snapshot(tmp_path, "replay", resume=True)
    assert result["applied"] == ["src/x.txt"]
    monkeypatch.undo()
    # Phase 3: a journal with an accounted plan auto-finalizes; the follow-up
    # fresh run is an idempotent no-op, not a permanent block.
    fresh = iso.integrate_snapshot(tmp_path, "replay")
    assert fresh["applied"] == []
    assert list(JR.iterdir()) == []
    assert (tmp_path / "src/x.txt").read_text() == "X-EDITED"


def test_reintegrate_after_success_is_idempotent_but_protects_later_edits(
    tmp_path: Path,
) -> None:
    """probe6(0) + don't-overwrite-later-edits."""
    _write(tmp_path / "src" / "x.txt", "X")
    env = _snapshot(tmp_path, ["src"], execution_id="idem")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/x.txt", "X-EDITED")
    first = iso.integrate_snapshot(tmp_path, "idem")
    assert first["applied"] == ["src/x.txt"]
    second = iso.integrate_snapshot(tmp_path, "idem")
    assert second["applied"] == []
    assert second["requires_verification"] is False
    assert (tmp_path / "src/x.txt").read_text() == "X-EDITED"
    # If the destination later moves on differently, a fresh overwrite is a
    # conflict, never a silent clobber.
    _write(tmp_path / "src/x.txt", "USER LATER EDIT")
    _write(files_dir / "src/x.txt", "X-EDITED-2")
    with pytest.raises(ConflictError):
        iso.integrate_snapshot(tmp_path, "idem")
    assert (tmp_path / "src/x.txt").read_text() == "USER LATER EDIT"


def test_resumed_replay_never_overwrites_intervening_user_edits(tmp_path: Path) -> None:
    """probe4: user edits BOTH files after an interruption; resume must keep them."""
    env_path_applied = "src/applied.txt"
    env_path_pending = "src/pending.txt"
    _write(tmp_path / env_path_applied, "ORIG-A")
    _write(tmp_path / env_path_pending, "ORIG-B")
    env = _snapshot(tmp_path, ["src"], execution_id="replay-user")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / env_path_applied, "ENV-A")
    _write(files_dir / env_path_pending, "ENV-B")
    real_apply = iso._apply_one
    state = {"applied": 0}

    def flaky(project: Path, staging_dir: Path, entry: dict) -> None:
        if entry["rel"] == env_path_pending and state["applied"] == 0:
            raise OSError("simulated crash between the two applies")
        return real_apply(project, staging_dir, entry)

    try:
        iso._apply_one = flaky
        with pytest.raises(iso.IntegrationInterrupted):
            iso.integrate_snapshot(tmp_path, "replay-user")
        assert (tmp_path / env_path_applied).read_text() == "ENV-A"
        # User edits after the interruption:
        _write(tmp_path / env_path_applied, "USER EDITED APPLIED FILE ITSELF")
        _write(tmp_path / env_path_pending, "USER EDITED PENDING FILE")
        journal_dir = next((Path(env["snapshot_dir"]) / ".journals").iterdir())
        journal = json.loads((journal_dir / "journal.json").read_text())
        assert [i["rel"] for i in journal["applied"]] == [env_path_applied]
        assert [i["rel"] for i in journal["pending"]] == [env_path_pending]
        iso._apply_one = real_apply
        with pytest.raises(ConflictError) as excinfo:
            iso.integrate_snapshot(tmp_path, "replay-user", resume=True)
        assert env_path_pending in str(excinfo.value)
        assert (tmp_path / env_path_applied).read_text() == "USER EDITED APPLIED FILE ITSELF"
        assert (tmp_path / env_path_pending).read_text() == "USER EDITED PENDING FILE"
    finally:
        iso._apply_one = real_apply


def test_parent_symlink_escape_at_integrate_rejected(tmp_path: Path, symlink_capable) -> None:
    """probe5(c)/probe7(3): symlinked dest parent escaping project never used."""
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"], execution_id="linksdir")
    outside = tmp_path.parent / "iso-outside-dir"
    outside.mkdir(parents=True, exist_ok=True)
    # Destination-side attack: a symlinked directory inside the snapshot scope
    # pointing outside the project, plus a manifest that would write through it.
    (tmp_path / "src" / "leak").symlink_to(outside)
    files_dir = Path(env["snapshot_dir"]) / "files"
    (files_dir / "src" / "leak").mkdir()
    _write(files_dir / "src/leak/payload.txt", "EVIL")
    registry_path = iso._registry_path(tmp_path)
    registry = json.loads(registry_path.read_text())
    registry["executions"]["linksdir"]["base_manifest"]["src/leak/payload.txt"] = {
        "hash": "0" * 64,
        "mode": 0o644,
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    _write(files_dir / "src/a.py", "A-EDITED")
    with pytest.raises(ValidationError) as excinfo:
        iso.integrate_snapshot(tmp_path, "linksdir")
    assert "escapes project" in str(excinfo.value)
    assert not (outside / "payload.txt").exists()
    # Restore the trusted manifest; the legitimate in-scope file still applies.
    shutil.rmtree(files_dir / "src" / "leak")  # tamper modeling done: clean the env
    registry = json.loads(registry_path.read_text())
    registry["executions"]["linksdir"]["base_manifest"].pop("src/leak/payload.txt")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    result = iso.integrate_snapshot(tmp_path, "linksdir")
    assert result["applied"] == ["src/a.py"]
    assert not (outside / "payload.txt").exists()


def test_explicit_scope_cannot_smuggle_cache_files(tmp_path: Path) -> None:
    """probe7(4): explicit pyc file scope is refused, not silently included."""
    _write(tmp_path / "src" / "ok.py", "A")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "m.pyc").write_bytes(b"\x00compiled")
    with pytest.raises(ValidationError):
        iso.create_snapshot(tmp_path, ["src/__pycache__/m.pyc"], execution_id="cache")
    with pytest.raises(ValidationError):
        iso.create_snapshot(tmp_path, ["src/__pycache__"], execution_id="cache2")
    # Legit scope is unaffected.
    env = iso.create_snapshot(tmp_path, ["src/ok.py"], execution_id="cache3")
    assert list(env["files"]) == ["src/ok.py"]


def test_fifo_socket_device_never_read(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    os.mkfifo(tmp_path / "src" / "pipe")
    env = _snapshot(tmp_path, ["src"], execution_id="fifo")
    assert "src/pipe" not in env["files"]
    assert "non-regular" in env["skipped"]["src/pipe"]
    # A FIFO placed in the env after snapshot is ignored at integrate, never opened.
    os.mkfifo(Path(env["snapshot_dir"]) / "files" / "src" / "pipe")
    (Path(env["snapshot_dir"]) / "files" / "src" / "a.py").unlink()  # deleted in env
    result = iso.integrate_snapshot(tmp_path, "fifo")
    assert result["applied"] == []
    assert "src/pipe" not in result["applied"]
    assert result["deleted"] == ["src/a.py"]
    assert not (tmp_path / "src/a.py").exists()
    # The destination's own FIFO is never touched or copied by integration.
    assert stat.S_ISFIFO(os.lstat(tmp_path / "src" / "pipe").st_mode)


def test_list_snapshots_corrupt_metadata_clear_error(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "A")
    iso.create_snapshot(tmp_path, ["src"], execution_id="corrupt")
    md = tmp_path / ".dado" / "local" / "isolation" / "corrupt" / "snapshot.json"
    md.write_text("{ not valid json !!!")
    with pytest.raises(ValidationError) as excinfo:
        iso.list_snapshots(tmp_path)
    assert "Corrupt snapshot metadata" in str(excinfo.value)
    with pytest.raises(ValidationError):
        iso.load_metadata(tmp_path, "corrupt")


def test_binding_matches_registry_snapshot_path(tmp_path: Path) -> None:
    """A metadata snapshot_dir disagreeing with the registry is refused."""
    _write(tmp_path / "src" / "a.py", "A")
    env = _snapshot(tmp_path, ["src"], execution_id="consistent")
    md_path = Path(env["snapshot_dir"]) / "snapshot.json"
    md = json.loads(md_path.read_text())
    md["snapshot_dir"] = str(tmp_path / "somewhere-else")
    md_path.write_text(json.dumps(md))
    with pytest.raises(ValidationError) as excinfo:
        iso.integrate_snapshot(tmp_path, "consistent")
    assert "does not match" in str(excinfo.value)


# --------------------------------------------------------------------------
# Exact-file scope matching (single-file snapshot scope regression).
# --------------------------------------------------------------------------


def test_exact_file_scope_modification_applies(tmp_path: Path) -> None:
    """Regression: file scope ['src/only.py'] must integrate its exact file."""
    _write(tmp_path / "src" / "only.py", "ONLY")
    _write(tmp_path / "src" / "unrelated.py", "UNRELATED")
    env = _snapshot(tmp_path, ["src/only.py"], execution_id="filescope")
    assert env["scope"] == ["src/only.py"]
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/only.py", "ONLY-EDITED")
    _write(tmp_path / "src/unrelated.py", "USER EDIT (retained)")
    result = iso.integrate_snapshot(tmp_path, "filescope")
    assert result["applied"] == ["src/only.py"]
    assert (tmp_path / "src/only.py").read_text() == "ONLY-EDITED"
    assert (tmp_path / "src/unrelated.py").read_text() == "USER EDIT (retained)"


def test_exact_file_scope_deletion_applies(tmp_path: Path) -> None:
    _write(tmp_path / "lib" / "only.py", "X")
    _write(tmp_path / "lib" / "other.py", "KEEP")
    env = _snapshot(tmp_path, ["lib/only.py"], execution_id="filedel")
    files_dir = Path(env["snapshot_dir"]) / "files"
    (files_dir / "lib/only.py").unlink()
    result = iso.integrate_snapshot(tmp_path, "filedel")
    assert result["deleted"] == ["lib/only.py"]
    assert not (tmp_path / "lib/only.py").exists()
    assert (tmp_path / "lib/other.py").read_text() == "KEEP"


def test_exact_file_scope_files_under_file_root_not_candidates(tmp_path: Path) -> None:
    """A file root matches exactly that file and nothing 'under' it."""
    _write(tmp_path / "src" / "only.py", "A")
    env = _snapshot(tmp_path, ["src/only.py"], execution_id="filenew")
    files_dir = Path(env["snapshot_dir"]) / "files"
    # The env file was replaced by a same-named DIRECTORY with files "below"
    # the file-root path: a directory cannot stand in for a file baseline, so
    # integration leaves it alone (no delete, no sneak-through).
    (files_dir / "src/only.py").unlink()
    (files_dir / "src/only.py").mkdir()
    _write(files_dir / "src/only.py" / "sneaky.txt", "NOPE")
    result = iso.integrate_snapshot(tmp_path, "filenew")
    assert result["applied"] == []
    assert result["deleted"] == []
    assert (tmp_path / "src/only.py").read_text() == "A"  # dest file untouched
    assert not (tmp_path / "src/only.py/sneaky.txt").exists()


def test_mixed_exact_file_and_directory_scope(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "run.sh", "X")
    (tmp_path / "docs" / "run.sh").chmod(0o755)
    _write(tmp_path / "src" / "pkg" / "a.py", "A")
    _write(tmp_path / "src" / "pkg" / "b.py", "B")
    env = _snapshot(tmp_path, ["docs/run.sh", "src/pkg"], execution_id="mixed")
    assert env["scope"] == ["docs/run.sh", "src/pkg"]
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "docs/run.sh", "Y")
    os.chmod(files_dir / "docs/run.sh", 0o755)
    expected_mode = stat.S_IMODE(os.stat(files_dir / "docs/run.sh").st_mode)
    _write(files_dir / "src/pkg/b.py", "B-EDITED")
    result = iso.integrate_snapshot(tmp_path, "mixed")
    assert sorted(result["applied"]) == ["docs/run.sh", "src/pkg/b.py"]
    assert stat.S_IMODE(os.stat(tmp_path / "docs/run.sh").st_mode) == expected_mode
    # Narrowing to one exact file of a mixed snapshot works.
    env2 = _snapshot(tmp_path, ["docs/run.sh", "src/pkg"], execution_id="mixed2")
    files2 = Path(env2["snapshot_dir"]) / "files"
    _write(files2 / "docs/run.sh", "Z")
    result2 = iso.integrate_snapshot(tmp_path, "mixed2", scope=["docs/run.sh"])
    assert result2["applied"] == ["docs/run.sh"]
    assert (tmp_path / "docs/run.sh").read_text() == "Z"
    with pytest.raises(ValidationError):
        iso.integrate_snapshot(tmp_path, "mixed2", scope=["docs/run.sh/sub"])  # under a file root


def test_discard_journal_is_bound_and_validates_snapshot_path(tmp_path: Path) -> None:
    """discard_journal follows the trusted binding, not the editable pointer."""
    _write(tmp_path / "src" / "a.txt", "A")
    env = _snapshot(tmp_path, ["src"], execution_id="bounddiscard")
    files_dir = Path(env["snapshot_dir"]) / "files"
    _write(files_dir / "src/a.txt", "A-EDITED")
    # Create an incomplete journal.
    real = iso._apply_one
    try:
        iso._apply_one = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        with pytest.raises(iso.IntegrationInterrupted):
            iso.integrate_snapshot(tmp_path, "bounddiscard")
    finally:
        iso._apply_one = real
    journals = Path(env["snapshot_dir"]) / ".journals"
    assert journals.exists()
    # A tampered metadata snapshot_dir must not redirect the removal.
    md_path = Path(env["snapshot_dir"]) / "snapshot.json"
    md = json.loads(md_path.read_text())
    victim_dir = tmp_path / "innocent-snapshot"
    shutil.copytree(str(env["snapshot_dir"]), str(victim_dir))
    tampered = dict(md, snapshot_dir=str(victim_dir))
    md_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        iso.discard_journal(tmp_path, "bounddiscard")
    assert "does not match" in str(excinfo.value)
    # The real (registered) .journals directory survived.
    assert list(journals.iterdir()), "trusted journal must not be deleted via tampered pointer"
    # Cross-project discard: an unknown id in the other checkout is refused
    # (with an explicit path the binding raises the 'belongs to project'
    # ConflictError, as integrate does).
    other = tmp_path.parent / "iso-discard-other"
    other.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValidationError):
        iso.discard_journal(other, "bounddiscard")
    # Restore the genuine metadata; bound discard works.
    md_path.write_text(json.dumps(md), encoding="utf-8")
    discarded = iso.discard_journal(tmp_path, "bounddiscard")
    assert discarded["discarded"]  # journal id recorded
    assert not journals.exists()  # trusted .journals removed
    assert (tmp_path / "src/a.txt").read_text() == "A"  # dest untouched by discard
    # Fresh integrate after discard still applies the environment cleanly.
    result = iso.integrate_snapshot(tmp_path, "bounddiscard")
    assert result["applied"] == ["src/a.txt"]
