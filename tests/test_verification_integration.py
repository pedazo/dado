import sys

import pytest

from dado.cli import build_parser
from dado.errors import ValidationError
from dado.evidence import capture_check
from dado.fs import file_lock, read_yaml, write_yaml
from dado.store import Store
from dado.verification import assert_task_evidence, record_integration


def started(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("value = 1\n")
    store = Store(root)
    path = store.create_work("Evidence", "Verify behavior")
    store.add_requirement(path.name, {"id": "REQ-001", "description": "Behavior", "source": "user", "status": "pending", "acceptance": ["works"]})
    store.add_task(path.name, {"id": "T-001", "title": "Task", "goal": "Behavior", "satisfies": ["REQ-001"],
        "scope": {"allowed": ["src"], "forbidden": []}, "acceptance": ["works"], "verification": ["test"]})
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "Approved")
    store.set_task_state(path.name, "T-001", "ready")
    store.set_task_state(path.name, "T-001", "running")
    store.record_result(path.name, {"task": "T-001", "status": "completed", "changed": ["src/app.py"],
        "verification": {"passed": True, "checks": ["worker check"]}, "assumptions": [], "issues": [], "evidence": [], "summary": "done"})
    return store, path


def captured(store, path, subject, acceptance, code="pass", scopes=None):
    record = capture_check(store.project, path / "evidence" / "checks", [sys.executable, "-c", code],
        {"subject": subject, "acceptance": acceptance, "exit_code": 0}, scopes=scopes)
    return f"evidence/checks/{record['id']}.json"


def test_task_pass_is_code_bound_and_failed_check_cannot_pass(tmp_path):
    store, path = started(tmp_path)
    bad = captured(store, path, "T-001", ["works"], "raise SystemExit(1)", ["src"])
    with pytest.raises(ValidationError, match="did not succeed"):
        store.verify_task(path.name, "T-001", "PASS", ["checked"], checked_paths=["src/app.py"], captured_records=[bad])
    good = captured(store, path, "T-001", ["works"], scopes=["src"])
    store.verify_task(path.name, "T-001", "PASS", ["checked"], checked_paths=["src/app.py"], captured_records=[good])
    assert_task_evidence(store, path, store.load_tasks(path)[0])
    (tmp_path / "src" / "app.py").write_text("value = 2\n")
    with pytest.raises(ValidationError, match="changed since verification"):
        assert_task_evidence(store, path, store.load_tasks(path)[0])
    store.set_requirement_status(path.name, "REQ-001", "done")
    with pytest.raises(ValidationError, match="changed since verification"):
        store.archive(path.name)


def test_integration_required_and_stale_after_combined_code_change(tmp_path):
    store, path = started(tmp_path)
    store.verify_task(path.name, "T-001", "PASS", ["review"], checked_paths=["src/app.py"])
    store.set_requirement_status(path.name, "REQ-001", "done")
    config_path = tmp_path / ".dado" / "config.yaml"
    config = read_yaml(config_path)
    config["verification"]["require_integration"] = True
    write_yaml(config_path, config)
    with pytest.raises(ValidationError, match="integration"):
        store.set_work_status(path.name, "completed")
    ref = captured(store, path, path.name, ["REQ-001: works"])
    with file_lock(store.root / ".state"):
        record_integration(store, path, [ref])
    (tmp_path / "consumer.py").write_text("broken = True\n")
    with pytest.raises(ValidationError, match="changed since verification"):
        store.set_work_status(path.name, "completed")


def test_check_parser_accepts_options_after_work_id():
    args = build_parser().parse_args(["check", "WORK-example", "--task", "T-001", "--criterion", "works", "--", sys.executable, "-c", "pass"])
    assert args.task == "T-001"
    assert args.argv == [sys.executable, "-c", "pass"]


@pytest.mark.parametrize("scopes", [["src/*.py"], ["src/app.py", "src/future.py"]])
def test_fingerprint_includes_future_scope_members(tmp_path, scopes):
    from dado.verification import assert_snapshot, compact_snapshot, snapshot_scopes
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n")
    store = Store(tmp_path)
    mapped = store._live_snapshot_scopes(scopes)
    assert mapped == snapshot_scopes(tmp_path, scopes)
    before = compact_snapshot(tmp_path, mapped)
    (tmp_path / "src" / "future.py").write_text("value = 2\n")
    with pytest.raises(ValidationError, match="changed since verification"):
        assert_snapshot(tmp_path, before)
