"""Tests for local bounded evidence: snapshot, capture_check and verify_record.

Real subprocesses are used for capture checks (no mocks), including a child
that mutates code during the check, a child that hangs until the group timeout
kills it, and one that exceeds the bounded output cap. Git tests create a real
repository in a temporary directory; git-unavailable behavior is covered by
replacing PATH with a shim (a sleeping fake ``git``) or an empty directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dado.evidence import (
    RECORD_KIND,
    RECORD_VERSION,
    capture_check,
    load_record,
    record_integrity_hash,
    snapshot,
    verify_record,
)
from dado.errors import ConflictError, DadoError, ValidationError


def _write(path: Path, data: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def test_snapshot_is_deterministic_and_content_sensitive(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "docs" / "note.md", "hi\n")

    first = snapshot(project)
    second = snapshot(project)
    assert first["digest"] == second["digest"]
    assert first["files"]["app.py"]["sha256"] == first["files"]["app.py"]["sha256"]
    assert set(first["files"]) == {"app.py", "docs/note.md"}

    _write(project / "app.py", "A = 2\n")
    assert snapshot(project)["digest"] != first["digest"]


def test_snapshot_walk_excludes_metadata_caches_and_secrets(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "__pycache__" / "app.cpython-313.pyc", "bytecode")
    _write(project / ".dado" / "work" / "active" / "W.yaml", "state")
    _write(project / ".pytest_cache" / "v" / "cache", "pytest")
    _write(project / ".venv" / "lib" / "x.py", "venv")
    _write(project / "app" / ".env", "SECRET=1\n")
    _write(project / "keys" / "server.pem", "-----BEGIN")
    _write(project / "app" / "secrets" / "vault.key", "SECRET")
    _write(project / "app" / "notes.md", "ok\n")

    files = set(snapshot(project)["files"])
    assert files == {"app.py", "app/notes.md"}


def test_snapshot_scopes_restict_and_reject_escapes(tmp_path):
    project = tmp_path / "proj"
    _write(project / "top.py", "top\n")
    _write(project / "app" / "inner.py", "inner\n")

    files = set(snapshot(project, scopes=["app"])["files"])
    assert files == {"app/inner.py"}
    # absolute scope paths are accepted and normalized
    assert set(snapshot(project, scopes=[project / "app"])["files"]) == {"app/inner.py"}
    assert snapshot(project)["scopes"] == []

    with pytest.raises(ValidationError, match="escapes"):
        snapshot(project, scopes=["../outside"])
    with pytest.raises(ValidationError):
        snapshot(project, scopes=[""])
    with pytest.raises(ValidationError):
        snapshot(project, scopes=42)


def test_snapshot_never_follows_symlinks_and_hashes_targets(tmp_path, symlink_capable):
    project = tmp_path / "proj"
    inside_target = _write(project / "data.txt", "version 1\n")
    os.symlink(inside_target, project / "link.txt")
    outside = _write(tmp_path / "outside.txt", "secret-out v1\n")
    os.symlink(outside, project / "leak.txt")
    dir_link = project / "dirlink"
    dir_link.mkdir()
    os.symlink(tmp_path, dir_link / "to-tmp")

    entries = snapshot(project)["files"]
    assert entries["link.txt"]["kind"] == "symlink"
    assert entries["link.txt"]["target"] == str(inside_target)
    assert entries["leak.txt"]["kind"] == "symlink"
    assert entries["dirlink/to-tmp"]["kind"] == "symlink"
    assert set(entries) == {"data.txt", "link.txt", "leak.txt", "dirlink/to-tmp"}

    # An out-of-tree target's content must not enter the digest through the link.
    before = snapshot(project)
    outside.write_text("secret-out v2\n")
    assert snapshot(project)["digest"] == before["digest"]
    # The linked entry itself also stays stable when its in-tree target is edited.
    inside_target.write_text("version 2\n")
    after = snapshot(project)
    assert after["files"]["link.txt"] == before["files"]["link.txt"]
    assert after["digest"] != before["digest"]  # data.txt itself changed, as a tree file


def test_snapshot_git_mode_tracks_dirty_untracked_deleted_and_ignored(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "gone.py", "G = 0\n")
    (project / ".gitignore").write_text("gen/\n")
    _init_repo(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "base")

    baseline = snapshot(project)
    assert baseline["git"]["available"] is True
    assert "gone.py" in baseline["files"]

    # A generated, git-ignored output (like verifier caches) must not move the digest.
    (project / "gen").mkdir()
    (project / "gen" / "out.bin").write_bytes(b"\x00cache")
    ignored_state = snapshot(project)
    assert ignored_state["digest"] == baseline["digest"]
    assert not any(key.startswith("gen") for key in ignored_state["files"])

    _write(project / "app.py", "A = 2\n")           # dirty tracked
    _write(project / "extra.py", "new\n")            # untracked
    (project / "gone.py").unlink()                   # deleted tracked

    changed = snapshot(project)
    assert changed["digest"] != baseline["digest"]
    assert changed["files"]["gone.py"] == {"kind": "missing"}
    assert changed["files"]["extra.py"]["kind"] == "file"
    assert changed["files"]["app.py"]["sha256"] != baseline["files"]["app.py"]["sha256"]


def test_snapshot_falls_back_to_walk_on_real_git_timeout(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX executable shim is not supported on Windows")
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _git(project, "init")

    shim = tmp_path / "shim"
    shim.mkdir()
    sleep_binary = shutil.which("sleep")
    assert sleep_binary, "sleep must be present for the timeout shim"
    (shim / "git").write_text(f"#!/bin/sh\nexec {sleep_binary} 30\n")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim))

    started = time.monotonic()
    state = snapshot(project, git_timeout=0.4)
    assert state["git"]["available"] is False
    assert state["git"]["reason"] == "timeout"
    assert "app.py" in state["files"]
    assert time.monotonic() - started < 5  # no waiting for the sleeping shim


def test_snapshot_falls_back_to_walk_without_git_binary(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")

    empty_bin = tmp_path / "nobin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    state = snapshot(project)
    assert state["git"]["available"] is False
    assert state["git"]["reason"] == "git-missing"
    assert state["files"]["app.py"]["kind"] == "file"


def test_snapshot_rejects_invalid_project(tmp_path):
    with pytest.raises(ValidationError):
        snapshot(tmp_path / "missing")
    not_a_dir = _write(tmp_path / "file.py", "x")
    with pytest.raises(ValidationError):
        snapshot(not_a_dir)


# ---------------------------------------------------------------------------
# capture_check
# ---------------------------------------------------------------------------

def test_capture_check_records_and_verifies_clean_run(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    evidence_dir = tmp_path / "evidence"

    record = capture_check(
        project,
        evidence_dir,
        [sys.executable, "-c", "print('check ran')"],
        {"exit_code": 0},
        session={"agent": "dado-verifier", "work": "W-001"},
    )

    assert record["kind"] == RECORD_KIND
    assert record["schema_version"] == RECORD_VERSION
    assert record["session"] == {"agent": "dado-verifier", "work": "W-001"}
    assert record["observed"]["exit_code"] == 0
    assert record["observed"]["timed_out"] is False
    assert isinstance(record["observed"]["duration_seconds"], float)
    assert record["observed"]["cwd"] == str(project)
    assert record["result"]["criterion_met"] is True
    assert record["observed"]["before_snapshot"]["digest"] == record["observed"]["after_snapshot"]["digest"]
    assert len(record["observed"]["logs"]) == 2  # stdout and stderr references
    for ref in record["observed"]["logs"]:
        log_file = Path(record["evidence_dir"]) / ref["path"]
        assert log_file.is_file()
        assert ref["bytes"] == log_file.stat().st_size

    assert (evidence_dir / f"{record['id']}.json").exists()
    assert verify_record(record, project=project)["status"] == "pass"
    assert verify_record(load_record(evidence_dir / f"{record['id']}.json"), project=project)["status"] == "pass"
    assert record == load_record(evidence_dir / f"{record['id']}.json")


def test_capture_check_runs_command_in_declared_cwd(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    run_dir = tmp_path / "run-here"
    run_dir.mkdir()

    record = capture_check(
        project,
        tmp_path / "evidence",
        [sys.executable, "-c", "from pathlib import Path; Path('made.by.check').write_text('x')"],
        {"exit_code": 0},
        cwd=run_dir,
    )
    assert (run_dir / "made.by.check").read_text() == "x"
    assert record["observed"]["cwd"] == str(run_dir)
    assert record["declared"]["cwd"] == str(run_dir)


def test_capture_check_allows_ignored_generated_outputs(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    (project / ".gitignore").write_text("gen/\n")
    _init_repo(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "base")

    record = capture_check(
        project,
        tmp_path / "evidence",
        [sys.executable, "-c", "import pathlib; pathlib.Path('gen').mkdir(exist_ok=True);"
                               "pathlib.Path('gen/cache.bin').write_bytes(b'junk')"],
        {"exit_code": 0},
    )
    assert (project / "gen" / "cache.bin").exists()
    assert record["result"]["criterion_met"] is True
    assert verify_record(record, project=project)["status"] == "pass"


def test_capture_check_rejects_code_change_during_run(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    evidence_dir = tmp_path / "evidence"

    with pytest.raises(ConflictError, match="modified app.py"):
        capture_check(
            project,
            evidence_dir,
            [sys.executable, "-c", "import pathlib; p=pathlib.Path('app.py'); p.write_text(p.read_text()+'\\n')"],
            {"exit_code": 0},
        )
    # Stale attempts leave no record and no logs.
    assert list(evidence_dir.glob("*.json")) == []
    left = list((evidence_dir / "logs").glob("*")) if (evidence_dir / "logs").exists() else []
    assert left == []


def test_capture_check_rejects_new_file_during_run(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")

    with pytest.raises(ConflictError, match="added"):
        capture_check(
            project,
            tmp_path / "evidence",
            [sys.executable, "-c", "import pathlib; pathlib.Path('smuggled.py').write_text('x')"],
            {"exit_code": 0},
        )


def test_capture_check_records_failed_criterion_without_raising(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")

    record = capture_check(
        project,
        tmp_path / "evidence",
        [sys.executable, "-c", "import sys; print('bang'); sys.exit(3)"],
        {"exit_code": 0, "note": "declared expectation"},
    )
    assert record["observed"]["exit_code"] == 3
    assert record["result"]["criterion_met"] is False
    assert record["declared"]["criterion"] == {"exit_code": 0, "note": "declared expectation"}
    assert 'bang' in (Path(record["evidence_dir"]) / record["observed"]["logs"][0]["path"]).read_text()
    assert verify_record(record, project=project)["status"] == "pass"


def test_capture_check_without_an_exit_code_criterion_still_records(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")

    record = capture_check(project, tmp_path / "evidence", [sys.executable, "-c", "print('ok')"], None)
    assert record["result"]["criterion_met"] is None
    assert verify_record(record, project=project)["status"] == "pass"


def test_capture_check_kills_process_group_on_timeout(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")

    started = time.monotonic()
    record = capture_check(
        project,
        tmp_path / "evidence",
        [sys.executable, "-c", "import time; print('begin', flush=True); time.sleep(30)"],
        {"exit_code": 0},
        timeout=0.7,
    )
    elapsed = time.monotonic() - started
    assert record["observed"]["timed_out"] is True
    assert record["observed"]["exit_code"] not in (0, None)
    assert isinstance(record["observed"]["duration_seconds"], float)
    assert elapsed < 10
    assert record["observed"]["stdout_bytes"] > 0
    assert record["result"]["criterion_met"] is False


def test_capture_check_bounds_output_at_generation(tmp_path):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    cap = 100_000

    record = capture_check(
        project,
        tmp_path / "evidence",
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2_000_000);"
                               "sys.stderr.write('y')"],
        {"exit_code": 0},
        output_cap_bytes=cap,
    )
    stdout_ref = [r for r in record["observed"]["logs"] if r["path"].endswith("stdout.log")][0]
    stderr_ref = [r for r in record["observed"]["logs"] if r["path"].endswith("stderr.log")][0]
    assert stdout_ref["bytes"] == cap  # hard cap, never larger
    assert stderr_ref["bytes"] == 1
    assert record["observed"]["output_truncated"] is True
    assert verify_record(record, project=project)["status"] == "pass"


def test_capture_check_command_must_be_argv_never_shell(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    evidence_dir = tmp_path / "evidence"

    with pytest.raises(ValidationError, match="shell string"):
        capture_check(project, evidence_dir, "ls -la", None)
    with pytest.raises(ValidationError):
        capture_check(project, evidence_dir, [], None)
    with pytest.raises(ValidationError):
        capture_check(project, evidence_dir, ["echo", 42], None)
    with pytest.raises(ValidationError):
        capture_check(project, evidence_dir, ["echo", "x"], {"exit_code": "zero"})


def test_capture_check_reports_unrunnable_command(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(DadoError, match="cannot run command"):
        capture_check(project, tmp_path / "evidence",
                      ["/nonexistent/binary/tool", "--flag"], {"exit_code": 0})


# ---------------------------------------------------------------------------
# verify_record
# ---------------------------------------------------------------------------

def _captured(tmp_path, command, **kwargs):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    evidence_dir = tmp_path / "evidence"
    record = capture_check(project, evidence_dir, command, {"exit_code": 0}, **kwargs)
    return project, record


def test_verify_rejects_code_changed_after_capture(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])

    _write(project / "app.py", "A = 2\n")
    with pytest.raises(ConflictError, match="modified app.py"):
        verify_record(record, project=project)

    fresh = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    _write(fresh[0] / "added.py", "x\n")
    with pytest.raises(ConflictError, match="added added\\.py"):
        verify_record(fresh[1], project=fresh[0])


def test_verify_rejects_tampered_log(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    ref = record["observed"]["logs"][0]
    log = Path(record["evidence_dir"]) / ref["path"]
    log.write_text(log.read_text() + "PWNED\n")

    with pytest.raises(ConflictError, match="tampered"):
        verify_record(record, project=project)


def test_verify_rejects_log_moved_or_missing(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    ref = record["observed"]["logs"][1]
    (Path(record["evidence_dir"]) / ref["path"]).unlink()

    with pytest.raises(ConflictError, match="missing"):
        verify_record(record, project=project)


def test_verify_rejects_tampered_record(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])

    forged = {**record, "observed": {**record["observed"], "exit_code": 0}}
    forged["observed"]["exit_code"] = 5
    with pytest.raises(ConflictError, match="tampered"):
        verify_record(forged, project=project)

    stripped = dict(record)
    stripped.pop("integrity")
    with pytest.raises(ValidationError):
        verify_record(stripped, project=project)


def test_record_integrity_hash_is_stable_across_key_order(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    shuffled = {key: record[key] for key in reversed(list(record))}
    assert record_integrity_hash(shuffled) == record_integrity_hash(record)
    assert record["integrity"]["record_sha256"] == record_integrity_hash(record)
    assert len(record["integrity"]["record_sha256"]) == 64


def test_verify_validates_record_shape(tmp_path):
    for broken in (
        {},
        [],
        {"kind": "other", "schema_version": RECORD_VERSION, "id": "x", "created_at": "t",
         "project": "/p", "evidence_dir": "/e", "scopes": [], "declared": {}, "observed":
         {"command": [], "cwd": "/", "exit_code": 0, "logs": [], "before_snapshot":
             {"digest": "0" * 64, "files": {}}}, "result": {}, "integrity": {"record_sha256": "0" * 64}},
    ):
        with pytest.raises(ValidationError):
            verify_record(broken)


def test_verify_offline_mode_skips_the_code_tree(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])

    shutil.rmtree(project)
    report = verify_record(record, check_code=False)
    assert report["status"] == "pass"
    assert report["checked"] == ["record_integrity", "logs"]
    with pytest.raises(Exception):
        verify_record(record, check_code=True)


def test_verify_defaults_to_the_recorded_project_path(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    assert verify_record(record)["project"] == str(project)


def test_verify_rejects_log_referencing_outside_evidence_dir(tmp_path):
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])
    forged = json.loads(json.dumps(record))
    log_ref = forged["observed"]["logs"][0]
    log_ref["path"] = "../../../../etc/passwd"
    forged["integrity"]["record_sha256"] = record_integrity_hash(forged)
    with pytest.raises((ValidationError, ConflictError)):
        verify_record(forged, project=project)


def test_capture_check_keeps_evidence_out_of_the_fingerprint(tmp_path):
    # Evidence inside the project must not make capture/verify self-stale.
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    evidence_dir = project / ".work" / "evidence"

    record = capture_check(project, evidence_dir, [sys.executable, "-c", "print('in')"],
                           {"exit_code": 0}, record_id="self-contained")
    assert record["declared"]["exclude_paths"] == [".work/evidence"]
    assert not any(key.startswith(".work") for key in record["observed"]["before_snapshot"]["files"])
    assert verify_record(record, project=project)["status"] == "pass"


# ---------------------------------------------------------------------------
# persisted capture options and backend-mode diagnostics
# ---------------------------------------------------------------------------

def test_capture_check_persists_exclude_dirs_and_replays_on_verification(tmp_path):
    # Regression: exclude_dirs must be replayed at verify time so an unchanged
    # code tree (edits confined to excluded dirs) is not falsely stale.
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "docs" / "note.md", "v1\n")
    evidence_dir = tmp_path / "evidence"

    record = capture_check(
        project, evidence_dir, [sys.executable, "-c", "print('ok')"],
        {"exit_code": 0}, exclude_dirs=["docs"],
    )
    assert record["declared"]["exclude_dirs"] == ["docs"]
    assert "docs/note.md" not in record["observed"]["before_snapshot"]["files"]

    loaded = load_record(evidence_dir / f"{record['id']}.json")
    assert verify_record(loaded, project=project)["status"] == "pass"

    _write(project / "docs" / "note.md", "v2\n")          # excluded-dir mutation
    assert verify_record(loaded, project=project)["status"] == "pass"

    _write(project / "app.py", "A = 2\n")                 # included-code mutation
    with pytest.raises(ConflictError, match="modified app.py"):
        verify_record(loaded, project=project)


def test_exclude_dirs_must_be_plain_directory_names(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    evidence_dir = tmp_path / "evidence"

    for bad in ("docs", ["a/b"], ["."], [".."], [""], [42], "/abs/path"):
        with pytest.raises(ValidationError):
            capture_check(project, evidence_dir, ["echo", "x"], None,
                          exclude_dirs=bad)
    for bad in ("docs", ["a/b"], [""], [42]):
        with pytest.raises(ValidationError):
            snapshot(project, exclude_dirs=bad)
    # valid inputs normalize (dedupe + sort) and duplicate names collapse
    record = capture_check(project, evidence_dir, [sys.executable, "-c", "print('n')"],
                           None, exclude_dirs=["z-cache", "docs", "docs"])
    assert record["declared"]["exclude_dirs"] == ["docs", "z-cache"]


def test_capture_check_persists_and_replays_git_timeout(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX executable shim is not supported on Windows")
    # A slow git shim: replaying capture's git_timeout keeps verification fast.
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "extra.py", "u\n")

    shim = tmp_path / "shim"
    shim.mkdir()
    sleep_binary = shutil.which("sleep")
    assert sleep_binary
    (shim / "git").write_text(f"#!/bin/sh\nexec {sleep_binary} 30\n")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim))

    record = capture_check(
        project, tmp_path / "evidence",
        [sys.executable, "-c", "print('ok')"], {"exit_code": 0},
        git_timeout=0.4,
    )
    assert record["declared"]["git_timeout"] == 0.4
    assert record["observed"]["before_snapshot"]["git"]["available"] is False
    capture_done = time.monotonic()

    report = verify_record(record, project=project)  # replays the 0.4s timeout
    assert report["status"] == "pass"
    # Default 10s per git call would exceed this bound by far; replay keeps
    # verification bounded by what the record persisted.
    assert time.monotonic() - capture_done < 5


def test_verify_diagnoses_git_backend_change_explicitly(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write(project / "app.py", "A = 1\n")
    _write(project / "gone.py", "G = 0\n")
    _init_repo(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "base")

    record = capture_check(project, tmp_path / "evidence",
                           [sys.executable, "-c", "print('ok')"], {"exit_code": 0})
    assert record["observed"]["before_snapshot"]["git"]["available"] is True

    empty_bin = tmp_path / "nobin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    # No "code changed" claim: the backend flip makes fingerprints incomparable.
    with pytest.raises(ConflictError, match="git backend changed"):
        verify_record(record, project=project)


def test_verify_tolerates_records_older_than_new_optional_fields(tmp_path):
    # Records written before exclude_dirs/git_timeout persisted and before
    # before_snapshot carried git info verify unchanged: no stronger claims.
    project, record = _captured(tmp_path, [sys.executable, "-c", "print('ok')"])

    legacy = json.loads(json.dumps(record))
    legacy["declared"].pop("exclude_dirs", None)
    legacy["declared"].pop("git_timeout", None)
    legacy["observed"]["before_snapshot"].pop("git", None)
    legacy["integrity"] = {"record_sha256": record_integrity_hash(legacy)}

    report = verify_record(legacy, project=project)
    assert report["status"] == "pass"
    assert report["checked"] == ["record_integrity", "logs", "code"]

    # Same legacy record must still reject actual code changes after capture.
    fresh_project, fresh_record = _captured(tmp_path, [sys.executable, "-c", "print('ok2')"])
    _write(fresh_project / "app.py", "A = 9\n")
    with pytest.raises(ConflictError, match="modified app.py"):
        verify_record(fresh_record, project=fresh_project)
