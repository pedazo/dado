"""Transaction-journal integration regressions for the Store.

Covers: journaled create/save/requirement flows, archive crash-and-replay at
every move/index point, prepare-vs-apply ordering, project-wide capacity that
sums same-work AND other-work active tasks, ready() projections, the events
only after durable commit rule, trusted-root refusals and building-journal
ignorance in repair.
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import pytest

import dado.persistence as persistence
from dado.errors import ValidationError
from dado.fs import read_yaml
from dado.layout import initialize_layout
from dado.store import Store
from dado.transactions import content_hash


# --------------------------------------------------------------- helpers

def task(id, deps=(), status="pending", scope=("src/example.py",)):
    return {"id": id, "title": id, "goal": "Implement bounded behavior", "status": status,
            "satisfies": ["REQ-001"], "depends_on": list(deps),
            "scope": {"allowed": list(scope), "forbidden": []},
            "context_refs": [], "acceptance": ["Behavior works"],
            "verification": ["pytest"], "risk": "low", "attempts": 0}


def req():
    return {"id": "REQ-001", "description": "Acceptance contract", "status": "pending",
            "source": "user", "acceptance": ["Feature works"]}


def result_packet(task_id: str = "T-001", changed=()) -> dict:
    return {"task": task_id, "status": "completed", "changed": list(changed),
            "verification": {"passed": True, "checks": ["worker check"]},
            "assumptions": [], "issues": [], "evidence": [], "summary": "done"}


def set_capacity(store: Store, tmp_path: Path, value: int) -> None:
    if not (tmp_path / ".dado/config.yaml").exists():
        store.init()  # idempotent: creates config only if actually missing
    config = store.load_config()
    config["scheduler"]["max_parallel_workers"] = value
    write_yaml(tmp_path / ".dado/config.yaml", config)


from dado.fs import write_yaml  # noqa: E402  (kept next to set_capacity for readability)


def approved_multi(store: Store, tmp_path: Path, work_id: str,
                   tasks: list[tuple[str, str]]) -> Path:
    """Work with one requirement and the given (task id, scope) pairs, approved."""
    path = store.create_work(work_id, "Request")
    store.add_requirement(path.name, req())
    for task_id, scope in tasks:
        store.add_task(path.name, task(task_id, scope=(scope,)))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    return path


def launch(store: Store, path: Path | str, task_id: str) -> None:
    work_id = path.name if isinstance(path, Path) else path
    store.set_task_state(work_id, task_id, "ready")
    store.set_task_state(work_id, task_id, "running")


class SimulatedCrash(Exception):
    pass


def crash_during(monkeypatch, op_index: int, *, label_in: str | None = None,
                 label_end: str | None = None) -> None:
    """Patch apply() to raise after `op_index` executed ops of matching label.

    The foundation's per-operation inject hook fires after each executed
    operation, which gives exactly "crash mid-transaction with a pending
    journal and partially applied state on disk". Call at most once per test
    (monkeypatch undoes between tests); match by substring and/or suffix.
    """
    real_apply = persistence.FileTransaction.apply

    def fake(self, *, inject=None):
        def hook(tx, index):
            matched = ((label_in is not None and label_in in tx.label)
                       or (label_end is not None and tx.label.endswith(label_end)))
            if matched and index == op_index:
                raise SimulatedCrash(f"simulated crash after op {index} of {tx.label}")
        return real_apply(self, inject=hook)

    monkeypatch.setattr(persistence.FileTransaction, "apply", fake)


def journal_paths(store: Store) -> list[Path]:
    return sorted((store.root / ".txn").glob("*.json"))


# ------------------------------------------------------------ facade basics

def test_commit_skips_the_journal_for_empty_batches(tmp_path):
    store = Store(tmp_path)
    store.init()
    result = persistence.commit(store.root, label="nothing")
    assert result["skipped"] is True and result["applied"] == 0
    assert journal_paths(store) == []  # no journal for nothing-to-do


def test_journal_is_durable_before_any_destination_is_touched(tmp_path):
    store = Store(tmp_path)
    store.init()
    tx = persistence.FileTransaction.begin(store.root, label="order:probe")
    target = store.active / "probe.txt"
    assert tx.write(target, "payload") == str(target)
    # Captured and journaled in the building phase — nothing applied yet.
    assert not target.exists()
    raw = json.loads(tx.journal_path.read_text(encoding="utf-8"))
    assert raw["phase"] == "building"  # invisible to recovery until sealed
    tx.apply()
    assert target.read_text(encoding="utf-8") == "payload"
    assert json.loads(tx.journal_path.read_text(encoding="utf-8"))["phase"] == "committed"


# ------------------------------------------------------ creation crash/replay

def _events(store: Store) -> list[str]:
    path = store.root / "events.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_create_work_crash_replay_completes_the_registration(tmp_path, monkeypatch):
    store = Store(tmp_path)
    wid = "WORK-crashy-create"
    crash_during(monkeypatch, 0, label_in="create-work")
    with pytest.raises(SimulatedCrash):
        store.create_work("Crashy creation", "The request", work_id=wid)
    # Prepare state: work.yaml applied, request/index not; empty staged dirs remain.
    assert (store.active / wid / "work.yaml").exists()
    assert not (store.active / wid / "request.md").exists()
    assert (store.active / wid / "tasks").is_dir()
    assert read_yaml(store.index_path)["active"] == []
    # Events only after durable state commit: the crashed creation logged none.
    assert _events(store) == []
    monkeypatch.undo()

    inventory = Store(tmp_path).repair(apply=False)
    assert len(inventory) == 1 and inventory[0]["phase"] == "pending"
    assert inventory[0]["ops"] == 4  # work packet, request, identity, index

    replayed = Store(tmp_path).repair(apply=True)
    assert [entry["status"] for entry in replayed] == ["replayed"]
    finished = Store(tmp_path)
    path = finished.work_path(wid)
    assert (path / "request.md").read_text(encoding="utf-8").startswith("# Original user request")
    assert any(item["id"] == wid for item in finished.list_active())
    # Recovery replays files, not business events.
    assert _events(store) == []
    # Subsequent repair invocations are no-ops (journals committed).
    assert Store(tmp_path).repair(apply=True) == []


def test_save_tasks_crash_never_emits_its_event(tmp_path, monkeypatch):
    store = Store(tmp_path)
    wid = "WORK-crashy-tasks"
    crash_during(monkeypatch, 0, label_in="tasks:")
    with pytest.raises(SimulatedCrash):
        path = store.create_work("Crashy tasks", "Request", work_id=wid)
        store.add_requirement(wid, req())
        store.add_task(wid, task("T-001"))
    monkeypatch.undo()
    # The journal was partially applied before the crash; because nothing
    # committed, no task_created event was recorded.
    assert (store.active / wid / "tasks/T-001.yaml").exists()
    assert not any("task_created" in line for line in _events(store))
    replayed = Store(tmp_path).repair(apply=True)
    assert replayed and replayed[-1]["status"] in {"replayed", "verified"}
    fresh = Store(tmp_path)
    tasks = fresh.load_tasks(store.active / wid)
    assert [t["id"] for t in tasks] == ["T-001"]
    legacy_plan = store.active / wid / "plan.yaml"
    legacy_plan.write_text("version: 1\ntasks: []\n", encoding="utf-8")
    fresh.save_tasks(store.active / wid, tasks)
    assert read_yaml(legacy_plan)["tasks"][0]["id"] == "T-001"


# ------------------------------------------------------ archive crash/replay

def _cancelled_with_task(store: Store, work_id: str = "ArchiveCrash") -> Path:
    path = store.create_work(work_id, "Request")
    store.add_requirement(path.name, req())
    store.add_task(path.name, task("T-001"))
    store.set_work_status(path.name, "cancelled")
    return path


@pytest.mark.parametrize("op_index", [0, 1, 2, 3, 4, 5])
def test_archive_crash_replay_completes_every_move_and_the_index(tmp_path, monkeypatch, op_index):
    store = Store(tmp_path)
    path = _cancelled_with_task(store)
    wid = path.name
    year = str(datetime.date.today().year)
    # Five artifacts travel (identity, request, summary, task packet, work
    # packet) and the index write is the last op of the batch.
    file_order = ["identity.yaml", "request.md", "summary.md", "tasks/T-001.yaml", "work.yaml"]

    crash_during(monkeypatch, op_index, label_end=":move")
    with pytest.raises(SimulatedCrash):
        store.archive(wid)
    monkeypatch.undo()

    partial = store.root / "archive" / year / wid
    moved_files = sorted(item.relative_to(partial).as_posix()
                         for item in partial.rglob("*") if item.is_file())
    assert moved_files == file_order[:op_index + 1]
    if op_index < 5:
        # Prepare vs apply: the index write only lands with the batch.
        assert any(item["id"] == wid for item in read_yaml(store.index_path)["active"])
        assert not _events(store) or not any("work_archived" in line for line in _events(store))
    in_flight = Store(tmp_path).repair(apply=False)
    assert len(in_flight) == 1 and in_flight[0]["phase"] == "pending"
    assert in_flight[0]["ops"] == 6  # 5 per-file moves + the final index write

    replayed = Store(tmp_path).repair(apply=True)
    assert replayed[0]["status"] == ("verified" if op_index == 5 else "replayed")

    fresh = Store(tmp_path)
    assert fresh.list_active() == []
    with pytest.raises(ValidationError, match="No active work"):
        fresh.work_path(wid)
    complete = sorted(item.relative_to(partial).as_posix()
                      for item in partial.rglob("*") if item.is_file())
    assert complete == file_order
    assert read_yaml(partial / "work.yaml")["status"] == "cancelled"
    # The emptied source directory tree stays in place: staged/empty dirs are
    # preserved, no file or directory deletion happens.
    assert path.is_dir()
    assert (path / "tasks").is_dir() and (path / "evidence").is_dir()
    assert not [item for item in path.rglob("*") if item.is_file()]
    # No fabricated archive event: the original call never completed.
    assert not any("work_archived" in line for line in _events(store))
    # Idempotent: further repair changes nothing.
    assert Store(tmp_path).repair(apply=True) == []


def test_crashed_finalize_batch_is_retried_and_stale_journal_never_reverts(tmp_path, monkeypatch):
    store = Store(tmp_path)
    path = _cancelled_with_task(store, "FinalizeCrash")
    wid = path.name
    year = str(datetime.date.today().year)

    crash_during(monkeypatch, 0, label_end=":finalize")
    with pytest.raises(SimulatedCrash):
        store.archive(wid)
    monkeypatch.undo()
    assert read_yaml(path / "work.yaml")["status"] == "cancelled"  # finalize op applied
    assert not (path / "summary.md").exists()
    assert not any("work_archived" in line for line in _events(store))

    # The retry completes the archive: every artifact moved to the archive
    # destination (directories preserved as sides on the source, never
    # deleted) and the index is clean.
    archived = store.archive(wid)
    assert (archived / "summary.md").exists()
    assert store.list_active() == []

    # The stale pre-crash journal is divergent (work.yaml content changed and
    # later moved): replay must report conflict and touch nothing.
    report = store.repair(apply=True)
    assert len(report) == 1
    assert report[0]["status"] == "conflict"
    assert read_yaml(archived / "work.yaml")["status"] == "cancelled"
    assert sorted(item.name for item in archived.iterdir()) == [
        "evidence", "identity.yaml", "request.md", "summary.md", "tasks", "work.yaml"]


# --------------------------------------- project-wide capacity and scope

def test_project_capacity_sums_same_work_and_other_work_tasks(tmp_path):
    store = Store(tmp_path)
    set_capacity(store, tmp_path, 3)
    a = approved_multi(store, tmp_path, "Work A",
                       [("T-001", "one/**"), ("T-002", "two/**")])
    b = approved_multi(store, tmp_path, "Work B",
                       [("T-001", "three/**"), ("T-002", "four/**")])
    launch(store, a, "T-001")
    launch(store, a, "T-002")   # 2 active within the same work
    launch(store, b, "T-001")   # project total 3: the full configured cap
    # B's second task: within-work slots remain, but the project pool is 3/3
    # and B's OWN running task must be summed with the foreign ones.
    with pytest.raises(ValidationError, match="capacity"):
        launch(store, b, "T-002")
    assert read_yaml(b / "tasks/T-002.yaml")["status"] == "ready"  # untouched refusal

    set_capacity(store, tmp_path, 4)
    store.set_task_state(b.name, "T-002", "running")  # with one more slot the start succeeds
    assert read_yaml(b / "tasks/T-002.yaml")["status"] == "running"


def test_ready_filters_cross_work_scope_and_capacity(tmp_path):
    store = Store(tmp_path)
    a = approved_multi(store, tmp_path, "Work A", [("T-001", "one/**")])
    launch(store, a, "T-001")
    b = approved_multi(store, tmp_path, "Work B",
                       [("T-001", "one/inner"), ("T-002", "twoB/**"), ("T-003", "threeB/**")])
    # Scope: T-001 overlaps work A's running scope and is filtered out even
    # in the read-only projection. Capacity: 1 foreign active task leaves 2
    # of the default 3 slots.
    assert [t["id"] for t in store.ready(b.name)] == ["T-002", "T-003"]
    launch(store, b, "T-002")
    assert [t["id"] for t in store.ready(b.name)] == ["T-003"]
    launch(store, b, "T-003")
    assert store.ready(b.name) == []  # 2 same-work + 1 foreign active tasks = cap exhausted

    set_capacity(store, tmp_path, 4)  # one slot frees up, T-001 still scope-excluded
    assert store.ready(b.name) == []


# --------------------------------------- repair trust and journal phases

def _write_forged_journal(store: Store, tx_id: str, roots: list[str], dst: Path, label: str) -> Path:
    forged = {
        "id": tx_id,
        "phase": "pending",
        "label": label,
        "roots": roots,
        "ops": [{"op": "write", "dst": str(dst), "before": None,
                 "after": content_hash(b"overwritten"), "content": "overwritten"}],
    }
    journal = store.root / ".txn" / f"{tx_id}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
    return journal


def test_repair_refuses_journals_with_untrusted_roots(tmp_path):
    store = Store(tmp_path)
    store.init()
    store.create_work("Trust probe", "Request")
    foreign = tmp_path / "outside.txt"
    foreign.write_text("user data", encoding="utf-8")
    journal = _write_forged_journal(store, "deadbeefdeadbeefdeadbeef0001",
                                    [str(tmp_path)], foreign, "evil:roots")
    report = store.repair(apply=True)
    assert len(report) == 1 and report[0]["status"] == "untrusted"
    assert foreign.read_text(encoding="utf-8") == "user data"  # untouched
    assert journal.exists()  # never deleted; stays pending for inspection
    assert store.repair(apply=False)[0]["phase"] == "pending"


def test_repair_refuses_ops_outside_trusted_roots_even_with_lying_roots(tmp_path):
    store = Store(tmp_path)
    store.init()
    foreign = tmp_path / "sibling.txt"
    foreign.write_text("user data", encoding="utf-8")
    # Roots field claims the store root; the operation actually escapes it.
    journal = _write_forged_journal(store, "deadbeefdeadbeefdeadbeef0002",
                                    [str(store.root)], foreign, "evil:ops")
    report = store.repair(apply=True)
    assert len(report) == 1 and report[0]["status"] == "unreadable"
    assert foreign.read_text(encoding="utf-8") == "user data"
    assert store.repair(apply=False)[0]["phase"] == "pending"


def test_repair_ignores_building_journals_and_touches_nothing(tmp_path):
    store = Store(tmp_path)
    wid = store.create_work("Building probe", "Request").name
    # Simulate a creator that died mid-declaration: journal exists, phase
    # building, a write captured but never sealed nor applied.
    tx = persistence.FileTransaction.begin(store.root, label="half:declared")
    tx.write(store.active / wid / "stray.txt", "never applied")
    assert not (store.active / wid / "stray.txt").exists()

    inventory = store.repair(apply=False)
    assert [entry["phase"] for entry in inventory if entry["label"] == "half:declared"] == ["building"]

    report = store.repair(apply=True)
    status_by_phase = {entry["phase"]: entry["status"] for entry in report}
    assert status_by_phase.get("building") == "ignored-building"
    assert not (store.active / wid / "stray.txt").exists()  # never applied
    # The unsealed journal survives untouched: recovery cannot complete it.
    assert tx.journal_path.exists()


# ------------------------------------------------ events after durable commit

def test_verifier_events_and_companion_packets_commit_together(tmp_path, monkeypatch):
    store = Store(tmp_path)
    path = store.create_work("Event gate", "Request")
    wid = path.name
    store.add_requirement(wid, req())
    store.add_task(wid, task("T-001"))
    store.set_work_status(wid, "awaiting_approval")
    store.approve_work(wid, "User approved")
    launch(store, wid, "T-001")
    store.record_result(wid, result_packet())

    crash_during(monkeypatch, 0, label_in="verification:")
    with pytest.raises(SimulatedCrash):
        store.verify_task(wid, "T-001", "PASS", ["independent check"], checked_paths=[])
    monkeypatch.undo()
    events = _events(store)
    assert not any("verification_passed" in line for line in events)
    packet = read_yaml(path / "tasks/T-001.yaml")
    assert packet["status"] == "done" and packet["verdict"]["verdict"] == "PASS"

    replayed = store.repair(apply=True)
    assert replayed and replayed[-1]["status"] == "verified"
    # Still no event: replay restores durable state but never re-emits the
    # business record; the Store appends events only when its API call
    # completes its own commit.
    assert not any("verification_passed" in line for line in _events(store))


def test_legacy_verdict_and_task_transition_commit_as_one_journal(tmp_path, monkeypatch):
    store = Store(tmp_path)
    path = store.create_work("Legacy companion", "Request")
    write_yaml(path / "plan.yaml", {"version": 1, "tasks": []})
    store.add_requirement(path.name, req())
    store.add_task(path.name, task("T-001"))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    launch(store, path.name, "T-001")
    store.record_result(path.name, result_packet(changed=[]))
    crash_during(monkeypatch, 0, label_in="verification:")
    with pytest.raises(SimulatedCrash):
        store.verify_task(path.name, "T-001", "PASS", ["checked"], checked_paths=[])
    monkeypatch.undo()
    # Crash after the task packet op (the companion verdict packet op is not
    # yet applied) — the packet is final on disk but the legacy verdict file
    # is still missing.
    assert not (path / "verification/T-001.yaml").exists()
    packet = read_yaml(path / "tasks/T-001.yaml")
    assert packet["status"] == "done" and "verdict" not in packet

    replayed = store.repair(apply=True)
    assert replayed[-1]["status"] == "replayed"
    assert (path / "verification/T-001.yaml").exists()
    packet = read_yaml(path / "tasks/T-001.yaml")
    assert packet["status"] == "done" and "verdict" not in packet
    assert read_yaml(path / "verification/T-001.yaml")["verdict"] == "PASS"


# ------------------------------------------------ full multi-work close-out
def test_budget_reservation_is_required_at_running_admission(tmp_path):
    from dado import budget as budget_module

    store = Store(tmp_path)
    path = store.create_work("Budget admission", "Request")
    store.add_requirement(path.name, req())
    store.add_task(path.name, task("T-001"))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    store.set_task_state(path.name, "T-001", "ready")  # admission is registration, not execution
    budget_module.initialize(path / "budget.yaml", cost=None, time_minutes=None, retries=3)

    # Without a reservation the task cannot execute.
    with pytest.raises(ValidationError, match="reservation"):
        store.set_task_state(path.name, "T-001", "running")
    assert read_yaml(path / "tasks/T-001.yaml")["status"] == "ready"  # untouched refusal

    budget_module.update(path / "budget.yaml", "reserve", phase="T-001", cost=None, retries=0)
    store.set_task_state(path.name, "T-001", "running")
    assert read_yaml(path / "tasks/T-001.yaml")["status"] == "running"


def test_verify_task_renews_a_done_task_without_rerunning_implementation(tmp_path):
    store = Store(tmp_path)
    path = store.create_work("Renewal", "Request")
    store.add_requirement(path.name, req())
    store.add_task(path.name, task("T-001"))
    store.set_work_status(path.name, "awaiting_approval")
    store.approve_work(path.name, "User approved")
    launch(store, path.name, "T-001")
    store.record_result(path.name, result_packet())
    store.verify_task(path.name, "T-001", "PASS", ["checked"], checked_paths=[])
    first = read_yaml(path / "tasks/T-001.yaml")
    assert first["status"] == "done" and first["verdict"]["verdict"] == "PASS"

    # Explicit re-verification of an already done task: no implementation
    # rerun is required or performed; the binding is renewed in place.
    store.verify_task(path.name, "T-001", "PASS", ["renewed independent check"], checked_paths=[])
    renewed = read_yaml(path / "tasks/T-001.yaml")
    assert renewed["status"] == "done" and renewed["attempts"] == first["attempts"]
    assert renewed["verdict"]["checks"] == ["renewed independent check"]

    # A done task can only renew with PASS.
    with pytest.raises(ValidationError, match="re-verification must PASS"):
        store.verify_task(path.name, "T-001", "FAIL", ["invalid"], checked_paths=[])


def test_create_work_mints_transactional_identity(tmp_path):
    store = Store(tmp_path)
    first = store.create_work("Identity one", "Request")
    second = store.create_work("Identity two", "Request")
    legacy = read_yaml(first / "identity.yaml")
    assert set(legacy) == {"version", "project_uuid", "checkout_uuid", "work_uuid", "execution_uuid"}
    assert legacy["version"] == 1
    assert legacy["project_uuid"] is None and legacy["checkout_uuid"] is None  # legacy layout
    assert legacy["work_uuid"] != legacy["execution_uuid"]
    assert read_yaml(second / "identity.yaml")["work_uuid"] != legacy["work_uuid"]

    # layout2 checkouts carry the shared/project and local checkout UUIDs.
    layout2_root = tmp_path / "layout2"
    initialize_layout(layout2_root)
    fresh = Store(layout2_root)
    stamped = read_yaml(fresh.create_work("Identity layout2", "Request") / "identity.yaml")
    assert stamped["project_uuid"] and stamped["checkout_uuid"]
    assert stamped["project_uuid"] != stamped["checkout_uuid"]


def test_two_disjoint_works_complete_concurrently_and_close_out(tmp_path):
    store = Store(tmp_path)
    set_capacity(store, tmp_path, 2)
    a = approved_multi(store, tmp_path, "Work A", [("T-001", "one/**")])
    b = approved_multi(store, tmp_path, "Work B", [("T-001", "two/**")])
    launch(store, a, "T-001")
    launch(store, b, "T-001")  # disjoint cross-work scopes run at capacity 2

    store.record_result(a.name, result_packet(changed=["one/x.py"]))
    store.verify_task(a.name, "T-001", "PASS", ["checked"], checked_paths=["one/x.py"])
    store.set_requirement_status(a.name, "REQ-001", "done")
    store.set_work_status(a.name, "awaiting_approval")
    store.approve_work(a.name, "Re-approved close-out")
    store.set_work_status(a.name, "completed")
    archived = store.archive(a.name)
    assert read_yaml(archived / "work.yaml")["status"] == "completed"
    assert len(store.list_active()) == 1
    assert store.repair(apply=True) == []  # clean tree: nothing inflight
