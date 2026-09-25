from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dado.domain import invalidate_closure, ready_tasks, transition, validate_dag
from dado.domain import scope_matches
from dado.errors import ConflictError, ValidationError
from dado.fs import read_yaml
from dado.installer import install, uninstall
from dado.models import set_agent_model
from dado.models import catalog
from dado.store import Store


def task(id, deps=(), status="pending"):
    return {"id": id, "title": id, "goal": "Implement bounded behavior", "status": status,
            "satisfies": ["REQ-001"], "depends_on": list(deps),
            "scope": {"allowed": ["src/example.py"], "forbidden": []},
            "context_refs": [], "acceptance": ["Behavior works"],
            "verification": ["pytest"], "risk": "low", "attempts": 0}


def test_dag_dependencies_parallel_cycle_and_missing():
    second = task("T-002")
    second["scope"]["allowed"] = ["tests/example.py"]
    tasks = [task("T-001"), second, task("T-003", ["T-001", "T-002"])]
    assert len(ready_tasks(tasks, max_parallel=2)) == 2
    with pytest.raises(ValidationError, match="cycle"):
        validate_dag([task("T-001", ["T-002"]), task("T-002", ["T-001"])])
    with pytest.raises(ValidationError, match="missing"):
        validate_dag([task("T-001", ["T-099"])])


def test_scheduler_serializes_overlapping_file_scopes():
    first = task("T-001", status="running")
    overlap = task("T-002")
    independent = task("T-003")
    independent["scope"]["allowed"] = ["tests/test_example.py"]
    assert [t["id"] for t in ready_tasks([first, overlap, independent], 2)] == ["T-003"]
    glob = task("T-004", status="running")
    glob["scope"]["allowed"] = ["src*/*.py"]
    exact = task("T-005")
    exact["scope"]["allowed"] = ["srcy/a.py"]
    assert not ready_tasks([glob, exact], 2)


def test_scope_globs_do_not_cross_directory_boundaries():
    assert scope_matches("src/a.py", "src/*.py")
    assert not scope_matches("src/nested/a.py", "src/*.py")
    assert not scope_matches("tests/src/hack.py", "src/*.py")
    assert scope_matches("src/nested/a.py", "src/**/*.py")
    assert scope_matches(".env/example.py", ".env/*.py")
    assert scope_matches("srcy/a.py", "src*/*.py")


def test_transition_and_stale_propagation():
    tasks = [task("T-001", status="done"), task("T-002", ["T-001"], "done"), task("T-003", ["T-002"], "pending")]
    assert invalidate_closure(tasks, "T-001") == ["T-001", "T-002", "T-003"]
    assert all(x["status"] == "stale" for x in tasks)
    with pytest.raises(ValidationError):
        transition(tasks[0], "running")


def test_init_idempotent_preserves_existing_opencode_and_conflicts(tmp_path):
    (tmp_path / ".opencode/agents").mkdir(parents=True)
    (tmp_path / ".opencode/commands").mkdir()
    (tmp_path / ".opencode/tools").mkdir()
    (tmp_path / ".opencode/skills/team-skill").mkdir(parents=True)
    user_agent = tmp_path / ".opencode/agents/custom.md"
    user_agent.write_text("user agent")
    (tmp_path / ".opencode/commands/custom.md").write_text("user command")
    (tmp_path / ".opencode/tools/custom.ts").write_text("export const custom = true")
    (tmp_path / ".opencode/skills/team-skill/SKILL.md").write_text("team skill")
    (tmp_path / ".opencode/agents/dado-worker.md").write_text("user-owned conflict")
    store = Store(tmp_path)
    store.init()
    installed, conflicts = install(tmp_path)
    assert user_agent.read_text() == "user agent"
    assert (tmp_path / ".opencode/commands/custom.md").read_text() == "user command"
    assert (tmp_path / ".opencode/tools/custom.ts").read_text() == "export const custom = true"
    assert (tmp_path / ".opencode/skills/team-skill/SKILL.md").read_text() == "team skill"
    assert "user-owned conflict" in (tmp_path / ".opencode/agents/dado-worker.md").read_text()
    assert any("dado-worker.md" in c for c in conflicts)
    installed2, conflicts2 = install(tmp_path)
    assert installed == installed2
    assert len(conflicts) == len(conflicts2)
    removed, preserved = uninstall(tmp_path)
    assert "user-owned conflict" in (tmp_path / ".opencode/agents/dado-worker.md").read_text()
    assert "agents/dado-worker.md" not in removed


def test_managed_customized_file_not_silently_overwritten(tmp_path):
    Store(tmp_path).init()
    install(tmp_path)
    agent = tmp_path / ".opencode/agents/dado-explorer.md"
    agent.write_text(agent.read_text() + "\n# personal customization\n")
    _, conflicts = install(tmp_path)
    assert any("modified managed" in c for c in conflicts)
    assert "personal customization" in agent.read_text()


def test_uninstall_never_deletes_manifest_paths_outside_templates(tmp_path):
    Store(tmp_path).init()
    install(tmp_path)
    protected = tmp_path / "protected.txt"
    protected.write_text("must remain")
    manifest = tmp_path / ".dado/managed-files.json"
    data = json.loads(manifest.read_text())
    data["../protected.txt"] = "irrelevant"
    manifest.write_text(json.dumps(data))
    _, preserved = uninstall(tmp_path)
    assert protected.read_text() == "must remain"
    assert any("invalid manifest path" in item for item in preserved)


def test_missing_config_is_a_clear_reinit_error(tmp_path):
    store = Store(tmp_path)
    store.init()
    (tmp_path / ".dado/config.yaml").unlink()
    with pytest.raises(ValidationError, match="run `dado init`"):
        store.load_config()


def test_git_project_init_adds_only_missing_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    config = tmp_path / ".opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"agents":{"custom":{"mode":"primary"}}}')
    Store(tmp_path).init()
    install(tmp_path)
    assert config.read_text() == '{"agents":{"custom":{"mode":"primary"}}}'
    assert (tmp_path / ".opencode/agents/dado-master.md").is_file()


def test_model_catalog_validation_and_frontmatter_preservation(tmp_path):
    file = tmp_path / "agent.md"
    file.write_text("---\n# keep this comment\ndescription: hi\nmodel: provider/old\nmode: subagent\n---\nbody\n")
    with pytest.raises(ValidationError):
        set_agent_model(file, "provider/missing", ["provider/current"])
    set_agent_model(file, "provider/current", ["provider/current"])
    assert "# keep this comment" in file.read_text()
    assert "description: hi" in file.read_text()
    assert "model: provider/current" in file.read_text()
    set_agent_model(file, None, ["provider/current"])
    assert "model:" not in file.read_text()


def test_models_uses_current_opencode_cli_output(monkeypatch):
    import subprocess
    def fake(command, **kwargs):
        assert command == ["opencode", "models"]
        return "provider/alpha\nprovider/beta#fast\nnot-a-model\n"
    monkeypatch.setattr(subprocess, "check_output", fake)
    assert catalog() == ["provider/alpha", "provider/beta#fast"]


def _add_req(store: Store, path: Path):
    store.add_requirement(path.name, {"id": "REQ-001", "description": "Acceptance contract", "status": "pending", "source": "user", "acceptance": ["Feature works"]})


def test_interrupted_work_recovery_full_acceptance_and_archive(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Acceptance feature", "Add a feature")
    work_id = path.name
    _add_req(store, path)
    first = task("T-001")
    second = task("T-002", ["T-001"])
    store.add_task(work_id, first)
    store.add_task(work_id, second)
    store.set_work_status(work_id, "awaiting_approval")
    store.approve_work(work_id, "User approved the reviewed two-task plan")

    # Task 1 implementation + independent verification are durable before interruption.
    store.set_task_state(work_id, "T-001", "ready")
    store.set_task_state(work_id, "T-001", "running")
    store.record_result(work_id, {"task": "T-001", "status": "completed", "changed": ["src/example.py"],
                                  "verification": {"passed": True, "checks": ["worker pytest"]}, "assumptions": [],
                                  "issues": [], "evidence": [], "summary": "First bounded implementation"})
    store.verify_task(work_id, "T-001", "PASS", ["pytest: passed"])

    # T-002 started just before process death. No result means uncertain, so retry only T-002.
    store.set_task_state(work_id, "T-002", "ready")
    store.set_task_state(work_id, "T-002", "running")
    recovered = Store(tmp_path).recover(work_id)
    assert recovered == ["T-002"]
    current = Store(tmp_path)
    assert next(t for t in current.load_tasks(path) if t["id"] == "T-001")["status"] == "done"
    assert [t["id"] for t in current.ready(work_id)] == ["T-002"]
    current.set_task_state(work_id, "T-002", "running")
    current.record_result(work_id, {"task": "T-002", "status": "completed", "changed": [],
                                    "verification": {"passed": True, "checks": ["worker check"]}, "assumptions": [],
                                    "issues": [], "evidence": [], "summary": "Second implementation"})
    current.verify_task(work_id, "T-002", "PASS", ["independent check passed"])
    work = read_yaml(path / "work.yaml")
    work["requirements"][0]["status"] = "done"
    from dado.fs import write_yaml
    write_yaml(path / "work.yaml", work)
    archived = current.archive(work_id)
    assert (archived / "summary.md").exists()
    assert not current.list_active()
    assert archived.joinpath("results/T-001.yaml").exists()
    assert "REQ-001" in (archived / "summary.md").read_text()


def test_worker_result_alone_cannot_mark_task_done(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Verify", "request")
    wid = path.name
    _add_req(store, path)
    store.add_task(wid, task("T-001"))
    store.set_work_status(wid, "awaiting_approval")
    store.approve_work(wid, "User approved")
    store.set_task_state(wid, "T-001", "ready")
    store.set_task_state(wid, "T-001", "running")
    store.record_result(wid, {"task": "T-001", "status": "completed", "changed": [],
                              "verification": {"passed": True, "checks": ["claimed pass"]}, "assumptions": [],
                              "issues": [], "evidence": [], "summary": "done"})
    assert store.load_tasks(path)[0]["status"] == "review"
    with pytest.raises(ValidationError, match="independent verifier"):
        store.set_task_state(wid, "T-001", "done")


def test_scope_violation_is_rejected_before_result_recording(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Scope", "request")
    wid = path.name
    _add_req(store, path)
    store.add_task(wid, task("T-001"))
    store.set_work_status(wid, "awaiting_approval")
    store.approve_work(wid, "User approved")
    store.set_task_state(wid, "T-001", "ready")
    store.set_task_state(wid, "T-001", "running")
    packet = {"task": "T-001", "status": "completed", "changed": ["outside.py"],
              "verification": {"passed": True, "checks": []}, "assumptions": [], "issues": [],
              "evidence": [], "summary": "claimed"}
    with pytest.raises(ValidationError, match="scope"):
        store.record_result(wid, packet)
    assert store.load_tasks(path)[0]["status"] == "running"


def test_concurrent_state_writes_are_serialized(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Concurrent", "request")
    wid = path.name
    _add_req(store, path)
    store.add_task(wid, task("T-001"))
    store.set_work_status(wid, "awaiting_approval")
    store.approve_work(wid, "User approved")
    store.set_task_state(wid, "T-001", "ready")
    def start():
        try:
            Store(tmp_path).set_task_state(wid, "T-001", "running")
            return "started"
        except ValidationError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: start(), range(2)))
    assert sorted(outcomes) == ["conflict", "started"]
    assert Store(tmp_path).load_tasks(path)[0]["status"] == "running"


def test_changed_plan_requires_reapproval(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Approval", "request")
    work_id = path.name
    _add_req(store, path)
    store.add_task(work_id, task("T-001"))
    store.set_work_status(work_id, "awaiting_approval")
    store.approve_work(work_id, "User approved initial plan")
    store.set_task_state(work_id, "T-001", "ready")
    (path / "spec.md").write_text("# Revised requirements\n")
    with pytest.raises(ValidationError, match="changed"):
        store.set_task_state(work_id, "T-001", "running")


def test_unchanged_approved_plan_can_resume_after_block(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Block recovery", "request")
    _add_req(store, path)
    store.add_task(path.name, task("T-001"))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    store.set_work_status(path.name, "blocked")
    store.set_work_status(path.name, "executing")
    store.set_task_state(path.name, "T-001", "ready")
    store.set_task_state(path.name, "T-001", "running")
    assert store.load_tasks(path)[0]["status"] == "running"


def test_default_approval_gate_blocks_worker_start(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Approval gate", "request")
    work_id = path.name
    store.add_task(work_id, task("T-001"))
    store.set_task_state(work_id, "T-001", "ready")
    with pytest.raises(ValidationError, match="explicit approval"):
        store.set_task_state(work_id, "T-001", "running")


def test_recovery_of_review_state_repeats_only_verifier(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Verifier recovery", "request")
    work_id = path.name
    _add_req(store, path)
    store.add_task(work_id, task("T-001"))
    store.set_work_status(work_id, "awaiting_approval")
    store.approve_work(work_id, "User approved")
    store.set_task_state(work_id, "T-001", "ready")
    store.set_task_state(work_id, "T-001", "running")
    store.record_result(work_id, {"task": "T-001", "status": "completed", "changed": ["src/example.py"],
                                  "verification": {"passed": True, "checks": ["worker check"]}, "assumptions": [],
                                  "issues": [], "evidence": [], "summary": "implementation persisted"})
    resumed = Store(tmp_path)
    assert resumed.recover(work_id) == ["T-001"]
    assert resumed.load_tasks(path)[0]["status"] == "review"
    assert not resumed.ready(work_id)
    resumed.verify_task(work_id, "T-001", "PASS", ["independent check"])
    assert resumed.load_tasks(path)[0]["status"] == "done"


def test_requirement_waiver_requires_rationale_and_is_auditable(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Waiver", "request")
    work_id = path.name
    _add_req(store, path)
    store.add_task(work_id, task("T-001"))
    store.set_work_status(work_id, "awaiting_approval")
    store.approve_work(work_id, "User approved")
    store.set_task_state(work_id, "T-001", "ready")
    store.set_task_state(work_id, "T-001", "running")
    store.record_result(work_id, {"task": "T-001", "status": "completed", "changed": ["src/example.py"],
                                  "verification": {"passed": True, "checks": ["worker check"]}, "assumptions": [],
                                  "issues": [], "evidence": [], "summary": "implemented"})
    store.verify_task(work_id, "T-001", "PASS", ["independent checks passed"])
    with pytest.raises(ValidationError, match="rationale"):
        store.set_requirement_status(work_id, "REQ-001", "waived")
    store.set_requirement_status(work_id, "REQ-001", "waived", "User explicitly removed this acceptance item")
    assert "Waive REQ-001" in (path / "decisions.md").read_text()
    store.set_work_status(work_id, "completed")
    assert store.archive(work_id).exists()


def test_active_context_paths_exclude_archive_and_other_work(tmp_path):
    store = Store(tmp_path)
    a = store.create_work("A", "request A")
    b = store.create_work("B", "request B")
    assert store.work_path(a.name) == a
    assert store.work_path(b.name) == b
    assert "archive" not in str(store.work_path(a.name))
    store.focus_work(a.name)
    assert read_yaml(store.index_path)["focused"] == a.name
    with pytest.raises(ValidationError, match="cold storage"):
        store.work_path("ARCHIVED-WORK")
