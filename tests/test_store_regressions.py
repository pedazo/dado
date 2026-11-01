"""Regressions for Store correctness (phase1) and runtime efficiencies (phase5).

Covers: spec.md section updates, schema validation before DAG logic,
cached schema validators with path+field diagnostics, packet writes that
skip unchanged YAML, configured-capacity defaults for ``ready``, project-wide
capacity/scope reservation across active works, and the git metadata timeout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dado.store as store_module
from dado import validation
from dado.errors import ValidationError
from dado.fs import read_yaml, write_yaml
from dado.store import Store


def task(id, deps=(), status="pending", scope=("src/example.py",)):
    return {"id": id, "title": id, "goal": "Implement bounded behavior", "status": status,
            "satisfies": ["REQ-001"], "depends_on": list(deps),
            "scope": {"allowed": list(scope), "forbidden": []},
            "context_refs": [], "acceptance": ["Behavior works"],
            "verification": ["pytest"], "risk": "low", "attempts": 0}


def req():
    return {"id": "REQ-001", "description": "Acceptance contract", "status": "pending",
            "source": "user", "acceptance": ["Feature works"]}


def approved_task(store: Store, tmp_path: Path, work_id: str, id: str = "T-001", scope=("src/example.py",)) -> Path:
    """Create a work with one requirement and one approved ready task."""
    path = store.create_work(work_id, "Request")
    store.add_requirement(path.name, req())
    store.add_task(path.name, task(id, scope=scope))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    store.set_task_state(path.name, id, "ready")
    return path


def set_capacity(store: Store, tmp_path: Path, value: int) -> None:
    config = store.load_config()
    config["scheduler"]["max_parallel_workers"] = value
    write_yaml(tmp_path / ".dado/config.yaml", config)


def result_packet(task_id: str = "T-001", changed=()) -> dict:
    return {"task": task_id, "status": "completed", "changed": list(changed),
            "verification": {"passed": True, "checks": ["worker check"]},
            "assumptions": [], "issues": [], "evidence": [], "summary": "done"}


def start(store: Store, work_id: str) -> None:
    store.set_task_state(work_id, "T-001", "running")


# -------------------------------------------------------------- spec.md

def test_requirement_status_updates_only_its_spec_section(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Spec sections", "Request")
    spec = path / "spec.md"
    spec.write_text(
        "# Spec\n\n"
        "## REQ-001 — First requirement\n\nSource: user\n\nStatus: pending\n\nAcceptance:\n- first\n\n"
        "## REQ-002 — Second requirement\n\nSource: user\n\nStatus: pending\n\nAcceptance:\n- second\n\n",
        encoding="utf-8")
    store.add_requirement(path.name, req())
    store.add_requirement(path.name, {"id": "REQ-002", "description": "Second requirement",
                                      "status": "pending", "source": "user", "acceptance": ["Second works"]})

    store.set_requirement_status(path.name, "REQ-001", "done")
    text = spec.read_text(encoding="utf-8")
    first, rest = text.split("\n## REQ-002", 1)
    assert "Status: done" in first
    assert "Status: pending" in rest  # the sibling section must be untouched

    # Transitions repeatedly rewrite the same single section.
    store.set_requirement_status(path.name, "REQ-002", "done")
    text = spec.read_text(encoding="utf-8")
    assert "Status: done" in text.split("\n## REQ-002", 1)[1]
    store.set_requirement_status(path.name, "REQ-002", "waived", "Dropped by user decision")
    assert "Status: waived" in spec.read_text(encoding="utf-8").split("\n## REQ-002", 1)[1]

    # Unknown requirement: error, and the document is left byte-identical.
    before = spec.read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="Unknown requirement"):
        store.set_requirement_status(path.name, "REQ-099", "blocked")
    assert spec.read_text(encoding="utf-8") == before


def test_spec_section_without_following_header_is_updated(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Spec trailing section", "Request")
    spec = path / "spec.md"
    # REQ-001 is the final section: no "\n## " header follows it.
    spec.write_text("# Spec\n\n## REQ-001 — Only\n\nSource: user\n\nStatus: pending\n\nAcceptance:\n- only\n",
                    encoding="utf-8")
    store.add_requirement(path.name, req())
    store.set_requirement_status(path.name, "REQ-001", "blocked")
    assert "Status: blocked" in spec.read_text(encoding="utf-8")


# ------------------------------------ schema validation before DAG logic

def test_malformed_depends_on_is_a_validation_error_not_a_traceback(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Malformed", "Request")
    bad = task("T-001")
    bad["depends_on"] = "T-001"  # a string instead of a list
    with pytest.raises(ValidationError, match="depends_on"):  # not TypeError
        store.save_tasks(path, [bad])
    bad["depends_on"] = None
    with pytest.raises(ValidationError, match="depends_on"):
        store.save_tasks(path, [bad])
    # Nothing was written: the failed packets never reached disk.
    assert list((path / "tasks").glob("*.yaml")) == []


def test_load_tasks_rejects_malformed_packet_fields(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Malformed packets", "Request")
    packet = task("T-001")
    packet["depends_on"] = "T-001"
    write_yaml(path / "tasks/T-001.yaml", packet)
    with pytest.raises(ValidationError, match="depends_on"):
        store.load_tasks(path)

    # An integer / pattern-breaking id cannot fail the DAG machinery either.
    write_yaml(path / "tasks/T-001.yaml", {"id": 1, "title": "broken"})
    with pytest.raises(ValidationError):  # packet id disagrees with filename
        store.load_tasks(path)


def test_save_tasks_validates_schema_before_dag(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Schema before dag", "Request")
    first, second = task("T-001"), task("T-002", ["T-001"])
    second["acceptance"] = []  # schema minItems violation, not a DAG error
    first["depends_on"] = ["T-002"]
    second["depends_on"] = ["T-001"]  # would also form a DAG cycle
    with pytest.raises(ValidationError, match="acceptance"):  # not "cycle"
        store.save_tasks(path, [first, second])
    # The invalid plan never reached disk.
    assert list((path / "tasks").glob("*.yaml")) == []


def test_save_tasks_invalid_id_pattern_is_clear_validation_error(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Bad id", "Request")
    bad = task("T-1")  # violates the ^T-[0-9]{3,}$ packet id pattern
    with pytest.raises(ValidationError, match="Invalid task"):
        store.save_tasks(path, [bad])


def test_embedded_result_and_verdict_must_belong_to_the_packet(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Foreign result", "Request")
    packet = task("T-001")
    packet["result"] = result_packet("T-002")
    write_yaml(path / "tasks/T-001.yaml", packet)
    with pytest.raises(ValidationError, match="refers to task"):
        store.load_tasks(path)
    packet["verdict"] = {"task": "T-009", "verdict": "PASS", "checks": ["x"], "evidence": [],
                         "verified_at": "2026-01-01T00:00:00+00:00"}
    packet["result"] = result_packet("T-001")
    write_yaml(path / "tasks/T-001.yaml", packet)
    with pytest.raises(ValidationError, match="refers to task"):
        store.load_tasks(path)
    # And the scope validator still runs on load.
    del packet["verdict"]
    packet["scope"] = {"allowed": ["../outside"], "forbidden": []}
    write_yaml(path / "tasks/T-001.yaml", packet)
    with pytest.raises(ValidationError, match="scope"):
        store.load_tasks(path)


# ------------------------------------- cached validators + diagnostics

def test_validator_cache_is_stable_per_kind_and_schema_file(tmp_path):
    validation.clear_cache()
    schemas = Store(tmp_path).schemas
    task_validator = validation.load_validator("task", schemas / "task.schema.json")
    assert validation.load_validator("task", schemas / "task.schema.json") is task_validator
    plan_validator = validation.load_validator("plan", schemas / "plan.schema.json")
    assert plan_validator is not task_validator

    copy = tmp_path / "task-copy.schema.json"
    copy.write_text((schemas / "task.schema.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert validation.load_validator("task", copy) is not task_validator
    validation.clear_cache()
    assert validation.load_validator("task", schemas / "task.schema.json") is not task_validator


def test_diagnostics_name_path_and_field_without_traceback(tmp_path):
    store = Store(tmp_path)
    bad = task("T-001")
    bad["status"] = "nope"  # enum violation at arrival, int inside a scope array
    bad["scope"] = {"allowed": [7], "forbidden": []}
    # Multiple errors whose JSON paths mix strings and integers must render
    # as diagnostics; the old mixed-type sort raised TypeError here.
    with pytest.raises(ValidationError) as caught:
        store._schema_validate("task", bad)
    message = str(caught.value)
    assert "Invalid task:" in message
    assert "status" in message
    assert "scope.allowed[0]" in message

    with pytest.raises(ValidationError) as scalar:
        store._schema_validate("task", "not-even-mapping")
    assert "Invalid task:" in str(scalar.value)


# ------------------------------------------- save_tasks change detection

def test_save_tasks_rewrites_only_truly_changed_packets(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Write instrumentation", "Request")
    write_yaml(path / "plan.yaml", {"version": 1, "tasks": []})  # legacy plan mode
    store.add_task(path.name, task("T-001"))
    store.add_task(path.name, task("T-002", ["T-001"], scope=("tests/example.py",)))

    mtime = (path / "tasks/T-002.yaml").stat().st_mtime_ns
    written = store.save_tasks(path, store.load_tasks(path))
    assert written == []  # nothing rewritten
    assert (path / "tasks/T-002.yaml").stat().st_mtime_ns == mtime
    # A no-op batch does not even start a journal.
    assert list((store.root / ".txn").glob("create*")) == []
    # The legacy plan index is also left untouched while its content matches.
    assert read_yaml(path / "plan.yaml")["tasks"][-1]["id"] == "T-002"

    tasks = store.load_tasks(path)
    tasks[0]["title"] = "Renamed"
    written = store.save_tasks(path, tasks)
    assert [packet.name for packet in written] == ["T-001.yaml"]
    assert read_yaml(path / "tasks/T-001.yaml")["title"] == "Renamed"
    assert (path / "tasks/T-002.yaml").stat().st_mtime_ns == mtime
    # The journal records the batch durably before applying (audit trail).
    assert list((store.root / ".txn").glob("*.json"))


def test_save_tasks_rewrites_the_legacy_plan_only_when_it_drifts(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Plan drift", "Request")
    write_yaml(path / "plan.yaml", {"version": 1, "tasks": []})
    store.add_task(path.name, task("T-001"))
    plan = path / "plan.yaml"
    consistent = plan.stat().st_mtime_ns
    store.save_tasks(path, store.load_tasks(path))
    assert plan.stat().st_mtime_ns == consistent  # its graph already agrees

    # Same parsed graph, different on-disk text: it is rewritten once into
    # the canonical serialization, then untouched again.
    plan.write_text("version: 1\ntasks:\n- {id: T-001, depends_on: []}\n", encoding="utf-8")
    drifted = plan.stat().st_mtime_ns
    store.save_tasks(path, store.load_tasks(path))
    assert plan.stat().st_mtime_ns != drifted
    assert read_yaml(plan)["tasks"][0]["id"] == "T-001"
    canonical = plan.stat().st_mtime_ns
    store.save_tasks(path, store.load_tasks(path))
    assert plan.stat().st_mtime_ns == canonical


# ------------------------------------------- configured capacity default

def test_ready_without_override_honors_configured_capacity(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Capacity default", "Request")
    store.add_requirement(path.name, req())
    store.add_task(path.name, task("T-001", scope=("src/**",)))
    store.add_task(path.name, task("T-002", scope=("docs/**",)))
    set_capacity(store, tmp_path, 1)
    assert [t["id"] for t in store.ready(path.name)] == ["T-001"]
    # An explicit override is capped at the configured capacity, never bypass.
    assert [t["id"] for t in store.ready(path.name, 99)] == ["T-001"]
    set_capacity(store, tmp_path, 3)
    assert [t["id"] for t in store.ready(path.name, 2)] == ["T-001", "T-002"]
    # A starting task still cannot exceed the configured project capacity.
    set_capacity(store, tmp_path, 1)  # capacity already fully used after one start
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    store.set_task_state(path.name, "T-001", "ready")
    store.set_task_state(path.name, "T-002", "ready")
    store.set_task_state(path.name, "T-001", "running")
    with pytest.raises(ValidationError, match="capacity"):
        store.set_task_state(path.name, "T-002", "running")


# -------------------------------- cross-work capacity and scope exclusion

def test_project_capacity_spanning_two_works(tmp_path):
    store = Store(tmp_path)
    a = approved_task(store, tmp_path, "Work A")
    b = approved_task(store, tmp_path, "Work B", scope=("tests/example.py",))
    set_capacity(store, tmp_path, 1)
    start(store, a.name)  # consumes the single project slot
    with pytest.raises(ValidationError, match="capacity"):
        start(store, b.name)
    set_capacity(store, tmp_path, 2)
    start(store, b.name)  # now both run side by side
    assert read_yaml(b / "tasks/T-001.yaml")["status"] == "running"
    assert read_yaml(a / "tasks/T-001.yaml")["status"] == "running"


def test_scope_exclusion_spans_two_works(tmp_path):
    store = Store(tmp_path)
    a = approved_task(store, tmp_path, "Work A", scope=("src/**",))
    b = approved_task(store, tmp_path, "Work B", scope=("src/other.py",))
    start(store, a.name)
    with pytest.raises(ValidationError, match="scope"):
        start(store, b.name)
    # A disjoint work starts without conflict under the default capacity.
    c = approved_task(store, tmp_path, "Work C", scope=("docs/**",))
    start(store, c.name)
    assert read_yaml(b / "tasks/T-001.yaml")["status"] == "ready"


def test_cross_work_overlap_error_identifies_the_conflicting_task(tmp_path):
    store = Store(tmp_path)
    a = approved_task(store, tmp_path, "Work A", scope=("src/**",))
    b = approved_task(store, tmp_path, "Work B", scope=("src/conflict.py",))
    start(store, a.name)
    with pytest.raises(ValidationError, match="T-001") as caught:
        start(store, b.name)
    assert a.name in str(caught.value)

    # With the default scope, Work A is already running; the configured
    # capacity of 3 leaves two slots, all counted across active works.
    set_capacity(store, tmp_path, 4)
    started = []
    for index in range(3):
        work = approved_task(store, tmp_path, f"Work{index}", scope=(f"dir{index}/**",))
        start(store, work.name)
        started.append(work)
    fourth = approved_task(store, tmp_path, "Work4", scope=("dir9/**",))
    with pytest.raises(ValidationError, match="capacity"):
        start(store, fourth.name)
    assert read_yaml(fourth / "tasks/T-001.yaml")["status"] == "ready"


# ----------------------------------------------- git metadata robustness

def test_git_metadata_uses_a_subprocess_timeout(tmp_path, monkeypatch):
    calls: dict = {}

    def fake_check_output(args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(store_module.subprocess, "check_output", fake_check_output)
    store = Store(tmp_path)
    assert store._git_metadata() == {"commit": None, "branch": None}
    assert calls["timeout"] == 10
