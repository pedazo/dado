"""Durable work artifact manager."""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
from collections.abc import Sequence
from pathlib import PurePosixPath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import budget, configuration, contracts, layout, persistence, validation, verification
from .domain import invalidate_closure, ready_tasks, scope_matches, scopes_overlap, transition, validate_dag
from .errors import ConflictError, ValidationError
from .fs import append_event, atomic_write, file_lock, read_yaml, write_yaml
from .transactions import TXN_DIRNAME


class Store:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.root = self.project / ".dado" / "work"
        self.active = self.root / "active"
        self.index_path = self.root / "index.yaml"
        self.schemas = Path(__file__).parent / "schemas"

    def init(self) -> None:
        self.active.mkdir(parents=True, exist_ok=True)
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "project").mkdir(parents=True, exist_ok=True)
        defaults = {
            self.root / "project" / "architecture.md": "# Project architecture\n\nPromote durable, sourced project facts here.\n",
            self.root / "project" / "conventions.md": "# Project conventions\n\nPromote verified conventions here.\n",
            self.root / "project" / "constraints.md": "# Project constraints\n\nRecord durable constraints with provenance.\n",
            self.root / "project" / "decisions.md": "# Project decisions\n\nDurable decisions only; include source work IDs.\n",
        }
        for path, content in defaults.items():
            if not path.exists():
                atomic_write(path, content)
        # Configuration bootstrap/validation is shared with the layout module
        # so a fresh CLI checkout (layout2) and this legacy path keep one
        # contract; legacy projects keep the direct-Store behavior.
        configuration.initialize(self.project)
        if not self.index_path.exists():
            write_yaml(self.index_path, {"focused": None, "active": []})
        self._validate_index()
        self.load_config()

    def load_config(self) -> dict[str, Any]:
        # Effective configuration differs under layout2 (profiles live in the
        # ignored local config); configuration.load applies that resolution,
        # while legacy checkouts keep the shared file as the single source.
        return configuration.load(self.project)

    def _schema_validate(self, kind: str, data: Any) -> None:
        # Draft 2020-12 validators are compiled once and cached in the
        # validation module; errors are rendered as clear path+field
        # diagnostics and can never surface as raw tracebacks.
        validation.diagnostics(kind, data, self.schemas / f"{kind}.schema.json")

    def _yaml_text(self, value: Any) -> str:
        """Serialize YAML exactly like dado.fs.write_yaml (single canonical form)."""
        return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=100)

    def _file_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else None
        except (OSError, UnicodeDecodeError):
            return None

    def _commit(self, label: str, writes: Sequence[tuple[Path, str]] = (),
                moves: Sequence[tuple[Path, Path]] = ()) -> dict[str, Any]:
        """Commit one durable batch under the store's journals root."""
        return persistence.commit(self.root, label=label, writes=writes, moves=moves)

    def _validate_index(self) -> dict[str, Any]:
        value = read_yaml(self.index_path)
        self._schema_validate("index", value)
        return value

    def create_work(self, title: str, request: str, work_id: str | None = None) -> Path:
        self.init()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:36] or "work"
        work_id = work_id or f"WORK-{stamp}-{slug}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,90}", work_id):
            raise ValidationError("Work ID may contain only letters, digits, '_' and '-' (3-91 chars)")
        path = self.active / work_id
        with file_lock(self.root / ".state"):
            if path.exists():
                raise ConflictError(f"Work already exists: {work_id}")
            for sub in ("tasks", "evidence"):
                (path / sub).mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).isoformat()
            git = self._git_metadata()
            work = {"id": work_id, "title": title, "status": "discovery", "created_at": now,
                    "updated_at": now, "base_commit": git.get("commit"), "branch": git.get("branch"),
                    "final_commit": None, "requirements": [], "decisions": []}
            self._schema_validate("work", work)
            request_text = (f"# Original user request\n\n{request.rstrip()}\n\n"
                            "## User clarifications and changes\n\n"
                            "<!-- Append dated user-provided changes. Do not rewrite original request. -->\n")
            index = self._validate_index()
            index["active"].append({"id": work_id, "path": f"active/{work_id}"})
            index["focused"] = work_id
            self._schema_validate("index", index)
            identity = self._identity(work_id)
            # Work packet, original request, transactional identity and the
            # discovery index commit as one journaled batch: a crash leaves a
            # pending journal that replays the whole registration or nothing.
            self._commit(f"create-work:{work_id}", writes=[
                (path / "work.yaml", self._yaml_text(work)),
                (path / "request.md", request_text),
                (path / "identity.yaml", self._yaml_text(identity)),
                (self.index_path, self._yaml_text(index)),
            ])
            # Events are ledger records for state that is already durable.
            append_event(self.root, "work_created", work=work_id)
        return path

    def _identity(self, work_id: str) -> dict[str, Any]:
        """Transactional identity for one work (layout2-aware, version 1).

        ``project_uuid``/``checkout_uuid`` come from the shared/local layout
        identity when the checkout runs layout2; a legacy layout has neither,
        so they stay null while the freshly minted work/execution UUIDs are
        always present.
        """
        project_uuid = checkout_uuid = None
        if layout.is_layout2(self.project):
            project_uuid = layout.project_uuid(self.project)
            checkout_uuid = layout.checkout_uuid(self.project)
        return {"version": 1, "project_uuid": project_uuid, "checkout_uuid": checkout_uuid,
                "work_uuid": layout.new_work_uuid(), "execution_uuid": layout.new_execution_uuid()}

    def list_active(self) -> list[dict[str, Any]]:
        # One index read serves the whole listing instead of a re-validated
        # index lookup per item.
        items = []
        for item in self._validate_index()["active"]:
            work = self.load_work(self._active_dir(item))
            items.append({**item, **{key: work[key] for key in ("title", "status", "created_at")}})
        return items

    def focus_work(self, work_id: str) -> None:
        # Validate active-path membership before changing the compact discovery index.
        self.work_path(work_id)
        with file_lock(self.root / ".state"):
            index = self._validate_index()
            index["focused"] = work_id
            write_yaml(self.index_path, index)
            append_event(self.root, "work_focused", work=work_id)

    def _active_dir(self, item: dict[str, Any]) -> Path:
        """Resolve one validated index entry, refusing escapes from active/."""
        path = (self.root / item["path"]).resolve()
        if not path.is_relative_to(self.active.resolve()):
            raise ValidationError("Work path escapes active directory")
        return path

    def work_path(self, work_id: str) -> Path:
        item = next((x for x in self._validate_index()["active"] if x["id"] == work_id), None)
        if not item:
            raise ValidationError(f"No active work {work_id}; archives are cold storage")
        return self._active_dir(item)

    def link_commit(self, work_id: str, revision: str) -> str:
        """Associate an existing Git commit with an explicitly selected completed archive."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,90}", work_id):
            raise ValidationError("Invalid work ID")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
            raise ValidationError("Commit must be a Git hash (7-64 hexadecimal characters)")
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                cwd=self.project, text=True, stderr=subprocess.DEVNULL, timeout=10,
            ).strip().lower()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("Commit does not exist in this project's Git repository") from exc
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValidationError("Git did not return a canonical commit hash")
        with file_lock(self.root / ".state"):
            matches = [p for p in (self.root / "archive").glob(f"*/{work_id}") if p.is_dir()]
            if len(matches) != 1:
                raise ValidationError(f"Expected one archived work {work_id}; found {len(matches)}")
            path = matches[0].resolve()
            if not path.is_relative_to((self.root / "archive").resolve()):
                raise ValidationError("Archive path escapes DADO work directory")
            work = self.load_work(path)
            if work["id"] != work_id or work["status"] != "completed":
                raise ValidationError("Only a completed archived work can be linked to a commit")
            commits = work.setdefault("related_commits", [])
            if sha not in commits:
                commits.append(sha)
                self._schema_validate("work", work)
                write_yaml(path / "work.yaml", work)
                append_event(self.root, "commit_linked", work=work_id, commit=sha)
        return sha

    def load_tasks(self, path: Path) -> list[dict[str, Any]]:
        packets = sorted((path / "tasks").glob("T-*.yaml"))
        tasks = [read_yaml(packet) for packet in packets]
        if any(not isinstance(task, dict) for task in tasks):
            raise ValidationError("Task packet is empty or corrupt")
        if any(task.get("id") != packet.stem for task, packet in zip(tasks, packets)):
            raise ValidationError("Task packet ID disagrees with its filename")
        # Every packet is schema-validated BEFORE validate_dag touches its
        # fields, so malformed values (a string or None depends_on, an integer
        # ID, ...) surface as clear validation errors instead of tracebacks.
        for task in tasks:
            self._validate_packet(task)
        if (path / "plan.yaml").exists():
            plan = read_yaml(path / "plan.yaml")
            self._schema_validate("plan", plan)
            graph = plan["tasks"]
            expected = {item["id"]: item["depends_on"] for item in graph}
            if len(expected) != len(graph) or expected != {t.get("id"): t.get("depends_on", []) for t in tasks}:
                raise ValidationError("Task packets and plan DAG disagree; preserve artifacts and reconcile manually")
        validate_dag(tasks)
        return tasks

    def _validate_packet(self, task: dict[str, Any]) -> None:
        """Schema-validate one packet, then its embedded result/verdict.

        Called before DAG logic in load_tasks and save_tasks. The packet ID is
        cross-checked against embedded result/verdict records so a record can
        never be silently attached to the wrong task.
        """
        task_id = task.get("id")
        try:
            self._schema_validate("task", task)
            for key, kind in (("result", "result"), ("verdict", "verification")):
                if key not in task:
                    continue
                payload = task[key]
                self._schema_validate(kind, payload)
                if isinstance(payload, dict) and payload.get("task") not in (None, task_id):
                    raise ValidationError(
                        f"{key} refers to task {payload.get('task')!r}; packet is {task_id!r}")
        except ValidationError as exc:
            raise ValidationError(f"Task {task_id}: {exc}") from exc
        self._validate_task_scope(task)

    def _validate_task_scope(self, task: dict[str, Any]) -> None:
        for entry in [*task.get("scope", {}).get("allowed", []), *task.get("scope", {}).get("forbidden", [])]:
            normalized = entry.replace("\\", "/")
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or (len(normalized) >= 2 and normalized[1] == ":"):
                raise ValidationError(f"Task {task.get('id')} scope must be a project-relative path/glob: {entry}")

    def load_work(self, path: Path) -> dict[str, Any]:
        work = read_yaml(path / "work.yaml")
        self._schema_validate("work", work)
        return work

    def add_requirement(self, work_id: str, requirement: dict[str, Any]) -> None:
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            work = read_yaml(path / "work.yaml")
            if any(item["id"] == requirement.get("id") for item in work["requirements"]):
                raise ConflictError(f"Requirement already exists: {requirement.get('id')}")
            work["requirements"].append(requirement)
            # Validate the mutated work (and therefore the requirement's
            # required fields) BEFORE rendering the spec section so a
            # malformed requirement yields a clear schema error, not a
            # KeyError traceback halfway through the update.
            self._schema_validate("work", work)
            writes = [(path / "work.yaml", self._yaml_text(work))]
            if (path / "spec.md").exists():
                spec = path / "spec.md"
                body = "\n".join(f"- {criterion}" for criterion in requirement["acceptance"])
                section = (f"\n## {requirement['id']} — {requirement['description']}\n\n"
                           f"Source: {requirement['source']}\n\nStatus: {requirement['status']}\n\nAcceptance:\n{body}\n")
                writes.append((spec, spec.read_text(encoding="utf-8").rstrip() + "\n" + section))
            # Work packet and its spec section stay consistent: both are
            # committed in a single journaled batch or neither happens.
            self._commit(f"requirements:{work_id}", writes=writes)
            append_event(self.root, "requirement_created", work=work_id, requirement=requirement["id"])

    def set_requirement_status(self, work_id: str, requirement_id: str, status: str, reason: str | None = None) -> None:
        if status not in {"pending", "done", "blocked", "waived"}:
            raise ValidationError(f"Invalid requirement status: {status}")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            work = read_yaml(path / "work.yaml")
            requirement = next((r for r in work["requirements"] if r["id"] == requirement_id), None)
            if requirement is None:
                raise ValidationError(f"Unknown requirement {requirement_id}")
            if status == "waived" and not (reason or "").strip():
                raise ValidationError("Waiving a requirement requires a concise rationale")
            requirement["status"] = status
            if status == "waived" and not (path / "decisions.md").exists():
                self._add_decision(work, f"Waive {requirement_id}", reason.strip(), [requirement_id], [])
            self._schema_validate("work", work)
            writes = [(path / "work.yaml", self._yaml_text(work))]
            if status == "waived" and (path / "decisions.md").exists():
                decisions = path / "decisions.md"
                content = decisions.read_text(encoding="utf-8")
                numbers = [int(n) for n in re.findall(r"^## DEC-(\d{3,})\b", content, re.M)]
                decision_id = f"DEC-{max(numbers, default=0) + 1:03d}"
                block = f"\n## {decision_id} — Waive {requirement_id}\n\nDate: {datetime.now(timezone.utc).date().isoformat()}\n\nRationale: {reason.strip()}\n\nRequirements: {requirement_id}\n\nTasks: none\n"
                writes.append((decisions, content.rstrip() + "\n" + block))
            if (path / "spec.md").exists():
                spec = path / "spec.md"
                content = spec.read_text(encoding="utf-8")
                marker = f"## {requirement_id} "
                start = content.find(marker)
                if start >= 0:
                    next_section = content.find("\n## ", start + len(marker))
                    end = next_section if next_section >= 0 else len(content)
                    section = content[start:end]
                    # Rebuild the document with the substituted status line.
                    # Previously the re.sub result was computed but discarded,
                    # so spec.md was rewritten unchanged.
                    updated = content[:start] + re.sub(r"(?m)^Status: .*?$", f"Status: {status}", section) + content[end:]
                    writes.append((spec, updated))
            # Work packet, discretionary decision record and spec section are
            # one consistent batch; nothing is written unless the whole
            # journal commits.
            self._commit(f"requirements-status:{work_id}", writes=writes)
            append_event(self.root, "requirement_status_changed", work=work_id, requirement=requirement_id, status=status)

    def _validate_contract(self, path: Path, work: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> None:
        if tasks is None:
            tasks = self.load_tasks(path)
        requirements = work.get("requirements", [])
        if not requirements:
            raise ValidationError("A plan must contain at least one stable requirement")
        req_ids = [r["id"] for r in requirements]
        if len(req_ids) != len(set(req_ids)):
            raise ValidationError("Work contains duplicate requirement IDs")
        if (path / "spec.md").exists():
            spec_text = (path / "spec.md").read_text(encoding="utf-8")
            for requirement in requirements:
                if not re.search(rf"(?m)^## {re.escape(requirement['id'])}(?:\s|$)", spec_text):
                    raise ValidationError(f"{requirement['id']} is missing from spec.md")
        waived = [r["id"] for r in requirements if r.get("status") == "waived"]
        decisions_text = (path / "decisions.md").read_text(encoding="utf-8") if (path / "decisions.md").exists() else "\n".join(d["text"] for d in work.get("decisions", []))
        for requirement_id in waived:
            if f"Waive {requirement_id}" not in decisions_text:
                raise ValidationError(f"Waived requirement {requirement_id} lacks its auditable decision/rationale")
        known = set(req_ids)
        covered = {req for task in tasks for req in task.get("satisfies", [])}
        unknown = covered - known
        if unknown:
            raise ValidationError(f"Tasks reference unknown requirements: {', '.join(sorted(unknown))}")
        missing = known - covered
        if missing:
            raise ValidationError(f"Requirements have no task coverage: {', '.join(sorted(missing))}")

    def save_tasks(self, path: Path, tasks: list[dict[str, Any]], extra_writes: Sequence[tuple[Path, str]] = (),
                   label: str = "") -> list[Path]:
        """Persist packets and the legacy plan; rewrite only changed files.

        ``extra_writes`` bundles companion records (legacy result/verdict
        packets, evidence attempt files) into the SAME journal so a transition
        and its companion record commit together; ``label`` names the journal
        (defaults to the generic ``tasks:<work>`` one). Returns the paths of
        task packets that were actually (re)written; unchanged packets keep
        their on-disk bytes and mtime and a no-op batch writes no journal at
        all.
        """
        written: list[Path] = []
        batch: list[tuple[Path, str]] = []
        # Validate schemas BEFORE validate_dag touches depends_on fields so
        # malformed packets produce clear errors instead of tracebacks.
        for task in tasks:
            self._validate_packet(task)
        validate_dag(tasks)
        # Task packets are the graph; old work keeps its legacy plan index.
        for task in tasks:
            packet = path / "tasks" / f"{task['id']}.yaml"
            serialized = self._yaml_text(task)
            if self._file_text(packet) != serialized:
                batch.append((packet, serialized))
                written.append(packet)
        if (path / "plan.yaml").exists():
            graph = [{"id": t["id"], "depends_on": t.get("depends_on", [])} for t in tasks]
            plan = {"version": 1, "tasks": graph}
            self._schema_validate("plan", plan)
            serialized = self._yaml_text(plan)
            if self._file_text(path / "plan.yaml") != serialized:
                batch.append((path / "plan.yaml", serialized))
        batch.extend(extra_writes)
        if batch:
            self._commit(label or f"tasks:{path.name}", writes=batch)
        return written

    def add_task(self, work_id: str, packet: dict[str, Any]) -> None:
        path = self.work_path(work_id)
        packet.setdefault("status", "pending")
        packet.setdefault("attempts", 0)
        packet.setdefault("risk", self.load_config().get("verification", {}).get("default_risk", "medium"))
        packet.setdefault("depends_on", [])
        packet.setdefault("satisfies", [])
        packet.setdefault("scope", {"allowed": [], "forbidden": []})
        packet.setdefault("context_refs", [])
        packet.setdefault("acceptance", [])
        packet.setdefault("verification", [])
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            if any(t["id"] == packet.get("id") for t in tasks):
                raise ConflictError(f"Task already exists: {packet['id']}")
            self._schema_validate("task", packet)
            self._validate_task_scope(packet)
            tasks.append(packet)
            self.save_tasks(path, tasks)
            append_event(self.root, "task_created", work=work_id, task=packet["id"])

    def set_task_state(self, work_id: str, task_id: str, state: str) -> None:
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            # One ID map serves every lookup in this method instead of
            # repeated next(...) scans over the task list.
            by_id = {t["id"]: t for t in tasks}
            task = by_id.get(task_id)
            if task is None:
                raise ValidationError(f"Unknown task {task_id}")
            work = self.load_work(path)
            if work["status"] in {"cancelled", "completed"} and state not in {"blocked", "failed", "cancelled", "stale"}:
                raise ValidationError(f"Cannot advance a task in terminal work state {work['status']}")
            if state == "done":
                raise ValidationError("A task can become done only through an independent verifier result")
            if state == "running":
                config = self.load_config()
                capacity = max(1, int(config.get("scheduler", {}).get("max_parallel_workers", 3)))
                self._assert_plan_approved(work_id, path, tasks=tasks, config=config)
                if task not in ready_tasks(tasks, capacity):
                    raise ValidationError(f"{task_id} is not runnable: capacity or scope conflict")
                # Reserve capacity and scope across ALL active works (local
                # project only) while the state lock is held, so the project
                # never runs more concurrent tasks than the configured cap:
                # same-work and other-work active tasks both consume slots.
                self._assert_project_capacity(path, task, tasks, capacity)
                # Works that opted into a budget ledger must hold an active
                # reservation for this task before it may execute.
                budget.require_reservation(path / "budget.yaml", task_id)
            if state == "review":
                worker_result = self._result(path, task)
                if worker_result.get("status") != "completed":
                    raise ValidationError("A completed worker Result Packet must exist before independent review")
            if state in {"ready", "running", "review", "done"} and any(by_id[d]["status"] != "done" for d in task.get("depends_on", [])):
                raise ValidationError(f"Dependencies are not complete for {task_id}")
            previous = task["status"]
            transition(task, state)
            extra_writes: list[tuple[Path, str]] = []
            superseded: tuple[Path, Path] | None = None
            if state == "running":
                task["attempts"] += 1
                task.pop("result", None)
                task.pop("verdict", None)
                task["started_contract_hash"] = self._task_contract_hash(path, task, work=work)
                # `work` was already read under this lock; reuse it instead of
                # re-reading work.yaml just to flip discovery into executing.
                if work["status"] in {"completed", "cancelled"}:
                    raise ValidationError(f"Cannot start a task in terminal work state {work['status']}")
                if work["status"] != "executing":
                    # Fold the executing flip into the same journal as the
                    # transition so task and work status cannot diverge on a
                    # crash.
                    work["status"] = "executing"
                    work["updated_at"] = datetime.now(timezone.utc).isoformat()
                    extra_writes.append((path / "work.yaml", self._yaml_text(work)))
                if (path / "plan.yaml").exists():
                    # Preserve superseded legacy packets without mistaking them for this run after a crash.
                    old_result = path / "results" / f"{task_id}.yaml"
                    old_verdict = path / "verification" / f"{task_id}.yaml"
                    if old_result.exists() or old_verdict.exists():
                        attempt = path / "evidence" / "attempts" / f"{task_id}-{task['attempts'] - 1:03d}.yaml"
                        extra_writes.append((attempt, self._yaml_text(
                            {"result": read_yaml(old_result), "verdict": read_yaml(old_verdict)})))
                        superseded = (old_result, old_verdict)
            self.save_tasks(path, tasks, extra_writes=extra_writes)
            if superseded is not None:
                # Cleanup of superseded legacy files is deliberately outside
                # the journal (the journal only knows write/move); if a crash
                # lands between commit and unlink, a later start repeats the
                # idempotent handoff exactly as the pre-journal flow did.
                superseded[0].unlink(missing_ok=True)
                superseded[1].unlink(missing_ok=True)
            if state == "running":
                append_event(self.root, "task_started", work=work_id, task=task_id, attempt=task["attempts"])
            append_event(self.root, "task_state_changed", work=work_id, task=task_id, previous=previous, status=state)

    def _assert_project_capacity(self, path: Path, task: dict[str, Any], tasks: list[dict[str, Any]],
                                 capacity: int) -> None:
        """Reserve project-wide capacity and scope for a task about to run.

        Called under the state lock, immediately before a task transitions to
        ``running``. The pool is the WHOLE project: running/review tasks of
        the same work (``tasks``, the already-validated snapshot) and of every
        other active work all consume configured capacity, and the starting
        task's allowed scope must not overlap the scope of any
        running/review task elsewhere.
        """
        # Same-work active tasks already hold slots; the starting task itself
        # is still pending/ready and counted through the +1 below.
        same_work = sum(t["status"] in {"running", "review"} for t in tasks)
        active_elsewhere = 0
        for item in self._validate_index()["active"]:
            neighbor = self._active_dir(item)
            if neighbor == path:
                continue
            for other in self.load_tasks(neighbor):
                if other["status"] not in {"running", "review"}:
                    continue
                active_elsewhere += 1
                if scopes_overlap(task.get("scope", {}).get("allowed", []),
                                  other.get("scope", {}).get("allowed", [])):
                    raise ValidationError(
                        f"{task['id']} conflicts with active task {other['id']} of work {item['id']} "
                        f"(project-wide scope exclusion): adjust the scope or wait for it to finish")
        if same_work + active_elsewhere + 1 > capacity:
            raise ValidationError(
                f"Project capacity is full: {same_work + active_elsewhere} of {capacity} concurrent "
                f"tasks are already running across active works; wait for one to finish or raise "
                f"scheduler.max_parallel_workers")

    def verify_task(self, work_id: str, task_id: str, verdict: str, checks: list[str], evidence: list[str] | None = None,
                    checked_paths: list[str] | None = None, captured_records: list[str] | None = None) -> None:
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
            raise ValidationError("Verifier verdict must be PASS, FAIL, or BLOCKED")
        if captured_records and verdict != "PASS":
            raise ValidationError("Captured evidence records apply only to PASS verification")
        for ref in evidence or []:
            self._validate_evidence_ref(ref, "Verifier evidence references")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            by_id = {t["id"]: t for t in tasks}
            task = by_id.get(task_id)
            if not task:
                raise ValidationError(f"Unknown task {task_id}")
            # An explicitly re-verified done task renews its binding without
            # re-running the implementation (PASS only).
            renewal = task["status"] == "done"
            if renewal and verdict != "PASS":
                raise ValidationError(f"{task_id} is already done; re-verification must PASS or mark it stale first")
            if not renewal and task["status"] != "review":
                raise ValidationError(f"{task_id} must be in review before independent verification")
            work = self.load_work(path)
            if work["status"] in {"cancelled", "completed"}:
                raise ValidationError("Cannot verify a task in a terminal work state")
            if verdict == "PASS":
                self._assert_plan_approved(work_id, path, tasks=tasks)
                self._assert_task_contract(path, task, work=work)
                if checked_paths is None:
                    raise ValidationError("PASS requires the verifier's independently checked paths (an empty list means no changes)")
                if sorted(set(checked_paths)) != sorted(set(self._result(path, task).get("changed", []))):
                    raise ValidationError("Verifier checked paths disagree with the worker result")
                self._check_scope(task, checked_paths)
            record = {"task": task_id, "verdict": verdict, "checks": checks, "evidence": evidence or [],
                      "verified_at": datetime.now(timezone.utc).isoformat()}
            if checked_paths is not None:
                record["checked_paths"] = checked_paths
            if verdict == "PASS":
                # A PASS verdict is code-bound: attach the scope snapshot (and
                # captured check coverage when provided) BEFORE any schema or
                # persistence step, so a stored PASS is never skippable.
                record.update(verification.task_evidence(self, path, self._fingerprint_task(task), captured_records))
            self._schema_validate("verification", record)
            extra_writes: list[tuple[Path, str]] = []
            if (path / "plan.yaml").exists():
                extra_writes.append((path / "verification" / f"{task_id}.yaml", self._yaml_text(record)))
            else:
                task["verdict"] = record
            if verdict == "PASS":
                task["status"] = "done"
                task["verified_at"] = record["verified_at"]
            else:
                task["status"] = "blocked" if verdict == "BLOCKED" else "failed"
            # The verification record and the task transition share one
            # journal: verdict file and packet status commit together.
            self.save_tasks(path, tasks, extra_writes=extra_writes, label=f"verification:{work_id}")
            # Done admission is validated against the durable verdict: the
            # code snapshot must still match the tree at admission time.
            if verdict == "PASS":
                self._assert_task_evidence(path, task)
            if verdict == "PASS" and renewal:
                append_event(self.root, "verification_renewed", work=work_id, task=task_id)
            elif verdict == "PASS":
                append_event(self.root, "verification_passed", work=work_id, task=task_id)
                append_event(self.root, "task_completed", work=work_id, task=task_id)
            else:
                append_event(self.root, "verification_failed", work=work_id, task=task_id, verdict=verdict)

    def _validate_evidence_ref(self, ref: str, description: str) -> None:
        ref_path = PurePosixPath(ref.replace("\\", "/"))
        if ref_path.is_absolute() or ".." in ref_path.parts or not ref_path.parts or ref_path.parts[0] != "evidence":
            raise ValidationError(f"{description} must be relative to this work's evidence/: {ref}")

    def _fingerprint_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Copy with conservative scope roots, including future matching files."""
        scope = task.get("scope", {}) or {}
        narrowed = dict(task)
        narrowed["scope"] = {"allowed": self._live_snapshot_scopes(scope.get("allowed", []) or []),
                             "forbidden": list(scope.get("forbidden", []) or [])}
        return narrowed

    def _live_snapshot_scopes(self, scopes: list[str]) -> list[str]:
        return verification.snapshot_scopes(self.project, scopes) or []

    def _assert_task_evidence(self, path: Path, task: dict[str, Any]) -> str:
        """Re-assert a task's recorded verification binding (done admission)."""
        try:
            return verification.assert_task_evidence(self, path, task)
        except FileNotFoundError as exc:
            raise ValidationError(
                f"The verified scope of {task['id']} no longer resolves ({exc}); verify again") from exc

    def record_result(self, work_id: str, result: dict[str, Any]) -> None:
        path = self.work_path(work_id)
        self._schema_validate("result", result)
        for ref in result.get("evidence", []):
            self._validate_evidence_ref(ref, "Evidence references")
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            by_id = {t["id"]: t for t in tasks}
            task = by_id.get(result["task"])
            if not task:
                raise ValidationError(f"Unknown task {result['task']}")
            if task["status"] != "running":
                raise ValidationError(f"Cannot record worker result while {task['id']} is {task['status']}; expected running")
            work = self.load_work(path)
            if work["status"] in {"cancelled", "completed"}:
                raise ValidationError("Cannot record a result in a terminal work state")
            self._assert_plan_approved(work_id, path, tasks=tasks)
            self._assert_task_contract(path, task, work=work)
            self._check_scope(task, result.get("changed", []))
            extra_writes: list[tuple[Path, str]] = []
            if (path / "plan.yaml").exists():
                extra_writes.append((path / "results" / f"{result['task']}.yaml", self._yaml_text(result)))
            else:
                task["result"] = result
            target = "review" if result["status"] == "completed" else "blocked" if result["status"] == "blocked" else "failed"
            transition(task, target)
            self.save_tasks(path, tasks, extra_writes=extra_writes, label=f"results:{work_id}")
            if target == "review":
                append_event(self.root, "result_recorded", work=work_id, task=task["id"])
            else:
                append_event(self.root, "task_blocked" if target == "blocked" else "task_failed", work=work_id, task=task["id"])

    def _check_scope(self, task: dict[str, Any], paths: list[str]) -> None:
        allowed = task.get("scope", {}).get("allowed", [])
        forbidden = task.get("scope", {}).get("forbidden", [])
        outside = []
        for changed in paths:
            normalized = changed.replace("\\", "/").removeprefix("./")
            changed_path = PurePosixPath(normalized)
            if changed_path.is_absolute() or ".." in changed_path.parts or (len(normalized) >= 2 and normalized[1] == ":"):
                outside.append(changed)
                continue
            is_allowed = any(scope_matches(normalized, pat) for pat in allowed)
            is_forbidden = any(scope_matches(normalized, pat) for pat in forbidden)
            if not is_allowed or is_forbidden:
                outside.append(changed)
        if outside:
            raise ValidationError(f"Result exceeds declared task scope; escalate for scope expansion: {', '.join(outside)}")

    def ready(self, work_id: str, max_parallel: int | None = None) -> list[dict[str, Any]]:
        """Runnable tasks of one work, projected against the WHOLE project.

        An explicit ``max_parallel`` is CAPPED at the configured capacity, it
        never bypasses ``scheduler.max_parallel_workers``; the default (None)
        uses the configured capacity. Beyond the usual within-work DAG, scope
        and sequence rules, candidates are filtered against running/review
        tasks of every OTHER active work: another work's active tasks consume
        capacity slots and exclude overlapping scope. This projection is
        advisory (no state lock); the authoritative reservation runs under
        the lock in ``set_task_state`` via ``_assert_project_capacity``.
        """
        path = self.work_path(work_id)
        config = self.load_config()
        configured = max(1, int(config.get("scheduler", {}).get("max_parallel_workers", 3)))
        limit = configured if max_parallel is None else max(1, min(configured, int(max_parallel)))
        if self.load_work(path)["status"] in {"cancelled", "completed"}:
            return []
        tasks = self.load_tasks(path)
        candidates = ready_tasks(tasks, limit)
        if not candidates:
            return []
        # Same-work active tasks consume slots too (already reflected in the
        # ready_tasks slot count); foreign active tasks are loaded once here.
        foreign_active: list[dict[str, Any]] = []
        for item in self._validate_index()["active"]:
            neighbor = self._active_dir(item)
            if neighbor == path:
                continue
            for other in self.load_tasks(neighbor):
                if other["status"] in {"running", "review"}:
                    foreign_active.append(other)
        slots = limit - sum(t["status"] in {"running", "review"} for t in tasks) - len(foreign_active)
        if slots <= 0:
            return []
        runnable = [task for task in candidates
                    if not any(scopes_overlap(task.get("scope", {}).get("allowed", []),
                                              other.get("scope", {}).get("allowed", []))
                               for other in foreign_active)]
        return runnable[:slots]

    def _result(self, path: Path, task: dict[str, Any]) -> dict[str, Any]:
        return task.get("result") or read_yaml(path / "results" / f"{task['id']}.yaml", {})

    def _verdict(self, path: Path, task: dict[str, Any]) -> dict[str, Any]:
        return task.get("verdict") or read_yaml(path / "verification" / f"{task['id']}.yaml", {})

    def set_work_status(self, work_id: str, status: str) -> None:
        if status not in {"discovery", "planning", "awaiting_approval", "executing", "blocked", "completed", "cancelled"}:
            raise ValidationError(f"Invalid work state: {status}")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            if status == "executing":
                self._assert_plan_approved(work_id, path)
            work = read_yaml(path / "work.yaml")
            self._schema_validate("work", work)
            allowed_transitions = {
                "discovery": {"planning", "awaiting_approval", "blocked", "cancelled"},
                "planning": {"discovery", "awaiting_approval", "blocked", "cancelled"},
                "awaiting_approval": {"planning", "executing", "cancelled"},
                "executing": {"planning", "awaiting_approval", "blocked", "completed", "cancelled"},
                "blocked": {"planning", "awaiting_approval", "executing", "completed", "cancelled"},
                "completed": set(),
                "cancelled": set(),
            }
            if status != work["status"] and status not in allowed_transitions.get(work["status"], set()):
                raise ValidationError(f"Invalid work transition: {work['status']} -> {status}")
            if status == "completed":
                tasks = self.load_tasks(path)
                self._assert_plan_approved(work_id, path, tasks=tasks)
                self._validate_contract(path, work, tasks=tasks)
                for task in tasks:
                    self._assert_task_contract(path, task, work=work)
                    # Admission to completed re-asserts the code-bound verdicts.
                    self._assert_task_evidence(path, task)
                if not tasks or any(t["status"] != "done" for t in tasks):
                    raise ValidationError("Work cannot be completed until all tasks are done")
                if any(self._verdict(path, t).get("verdict") != "PASS" for t in tasks):
                    raise ValidationError("Work cannot be completed without independent PASS evidence")
                if any(r["status"] not in {"done", "waived"} for r in work["requirements"]):
                    raise ValidationError("Work cannot be completed until requirements are done or explicitly waived")
                # Integration verification is required only where the config
                # demands it (legacy works declare it optional by default).
                verification.assert_integration(self, path)
            work["status"] = status
            work["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._schema_validate("work", work)
            write_yaml(path / "work.yaml", work)
            append_event(self.root, "work_status_changed", work=work_id, status=status)

    def promote_knowledge(self, work_id: str, target: str, text: str, source_ref: str) -> None:
        allowed = {"architecture.md", "conventions.md", "constraints.md", "decisions.md"}
        if target not in allowed:
            raise ValidationError(f"Project memory target must be one of {', '.join(sorted(allowed))}")
        if not text.strip() or not source_ref.strip():
            raise ValidationError("Promotion requires useful content and provenance")
        self.work_path(work_id)
        artifact = self.root / "project" / target
        with file_lock(self.root / ".state"):
            prior = artifact.read_text(encoding="utf-8")
            promotion = f"\n\n## Promoted from {work_id}\n\n{text.strip()}\n\nOrigin: `{work_id} / {source_ref}`\n"
            atomic_write(artifact, prior.rstrip() + promotion)
            append_event(self.root, "knowledge_promoted", work=work_id, target=target, source=source_ref)

    def record_decision(self, work_id: str, decision: str, rationale: str,
                        requirements: list[str] | None = None, tasks: list[str] | None = None) -> str:
        if not decision.strip() or not rationale.strip():
            raise ValidationError("Decision and concise rationale are required")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            if not (path / "decisions.md").exists():
                work = self.load_work(path)
                decision_id = self._add_decision(work, decision, rationale, requirements or [], tasks or [])
                write_yaml(path / "work.yaml", work)
                append_event(self.root, "decision_created", work=work_id, decision=decision_id)
                return decision_id
            file = path / "decisions.md"
            content = file.read_text(encoding="utf-8")
            existing = [int(n) for n in re.findall(r"^## DEC-(\d{3,})\b", content, re.M)]
            decision_id = f"DEC-{max(existing, default=0) + 1:03d}"
            date = datetime.now(timezone.utc).date().isoformat()
            reqs = ", ".join(requirements or []) or "none recorded"
            task_ids = ", ".join(tasks or []) or "none recorded"
            block = (f"\n## {decision_id} — {decision.strip()}\n\nDate: {date}\n\n"
                     f"Rationale: {rationale.strip()}\n\nRequirements: {reqs}\n\nTasks: {task_ids}\n")
            atomic_write(file, content.rstrip() + "\n" + block)
            append_event(self.root, "decision_created", work=work_id, decision=decision_id)
            return decision_id

    @staticmethod
    def _add_decision(work: dict[str, Any], text: str, rationale: str, requirements: list[str], tasks: list[str]) -> str:
        decisions = work.setdefault("decisions", [])
        decision_id = f"DEC-{len(decisions) + 1:03d}"
        decisions.append({"id": decision_id, "text": text, "rationale": rationale,
                          "date": datetime.now(timezone.utc).date().isoformat(),
                          "requirements": requirements, "tasks": tasks})
        return decision_id

    def invalidate(self, work_id: str, task_id: str, reason: str) -> list[str]:
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            affected = invalidate_closure(tasks, task_id)
            self.save_tasks(path, tasks)
            append_event(self.root, "task_invalidated", work=work_id, task=task_id, affected=affected, reason=reason)
            return affected

    def recover(self, work_id: str) -> list[str]:
        """Reconcile running/review work conservatively after restart."""
        # Finish any interrupted persistence FIRST: an archive that crashed
        # between moving artifacts and updating the index leaves the work
        # half-addressable, and `work_path` below must observe a consistent
        # post-replay state (either the re-deregistered work or its archive).
        self.repair(apply=True)
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            if self.load_work(path)["status"] in {"cancelled", "completed"}:
                return []
            tasks = self.load_tasks(path)
            by_id = {t["id"]: t for t in tasks}
            reset = []
            changed = False
            for task in tasks:
                if task["status"] == "running":
                    # If result reached disk just before a crash, preserve its handoff to verifier.
                    result = self._result(path, task)
                    if result.get("status") == "completed":
                        task["status"] = "review"
                    elif result.get("status") == "blocked":
                        task["status"] = "blocked"
                    elif result.get("status") == "failed":
                        task["status"] = "failed"
                    else:
                        # Without a durable result execution is uncertain; retry only this task.
                        task["status"] = "ready" if all(by_id[d]["status"] == "done" for d in task.get("depends_on", [])) else "pending"
                    reset.append(task["id"])
                    changed = True
                elif task["status"] == "review":
                    result = self._verdict(path, task)
                    if result.get("verdict") == "PASS":
                        self._schema_validate("verification", result)
                        task["status"] = "done"
                        changed = True
                    else:
                        # Keep the durable implementation in review so only verification is repeated.
                        reset.append(task["id"])
            if changed:
                self.save_tasks(path, tasks)
            if reset:
                append_event(self.root, "work_recovered", work=work_id, reconciled=reset)
            return reset

    def repair(self, apply: bool = False) -> list[dict[str, Any]]:
        """Inventory (and optionally replay) interrupted persistence journals.

        apply=False (default): read-only listing of unsealed journals under
        ``.dado/work/.txn`` — pending (sealed, recoverable), building
        (interrupted declarations, unreplayable by design) and unreadable
        ones. Committed journals are the audit trail and are not reported.

        apply=True: holds the state lock and replays every pending journal
        idempotently, trusting ONLY this store's own root. Divergent
        destinations are never overwritten: such journals stay pending and
        are reported with ``status="conflict"``. Building journals are only
        inventoried (``"ignored-building"``); they can never be completed by
        recovery. Nothing is deleted, rewound or guessed at.
        """
        if not (self.root / TXN_DIRNAME).is_dir():
            return []
        if not apply:
            return persistence.inventory(self.root)
        with file_lock(self.root / ".state"):
            return persistence.replay(self.root, trusted_roots=[self.root])

    def archive(self, work_id: str) -> Path:
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            dest = self.root / "archive" / str(datetime.now().year) / work_id
            if dest.exists():
                raise ConflictError(f"Archive destination already exists: {dest}")
            work = read_yaml(path / "work.yaml")
            tasks = self.load_tasks(path)
            self._schema_validate("work", work)
            if work["status"] != "cancelled":
                self._assert_plan_approved(work_id, path, tasks=tasks)
                self._validate_contract(path, work, tasks=tasks)
                for task in tasks:
                    self._assert_task_contract(path, task, work=work)
                    # Archival re-asserts each task's code-bound verdict and
                    # the (config-gated) integration verification.
                    self._assert_task_evidence(path, task)
                if not tasks or any(t["status"] != "done" for t in tasks):
                    raise ValidationError("Cannot archive: every task must be independently verified and done")
                for task in tasks:
                    verification_ = self._verdict(path, task)
                    if verification_.get("verdict") != "PASS":
                        raise ValidationError(f"Cannot archive: {task['id']} lacks independent PASS evidence")
                    self._schema_validate("verification", verification_)
                verification.assert_integration(self, path)
                if any(r.get("status") not in {"done", "waived"} for r in work.get("requirements", [])):
                    raise ValidationError("Cannot archive: all requirements must be done or explicitly waived")
                work["status"] = "completed"
            elif any(t["status"] in {"running", "review"} for t in tasks):
                raise ValidationError("Cannot archive cancelled work with running or reviewing tasks; reconcile them first")
            work["updated_at"] = datetime.now(timezone.utc).isoformat()
            if work["status"] == "completed":
                head = self._git_metadata().get("commit")
                work["final_commit"] = head if head != work.get("base_commit") else None
            summary = self._summary(work, tasks, path)
            # Batch 1 (safe finalize BEFORE any move): the final work packet
            # and the close-out summary commit as one journaled batch, so the
            # artifacts describe their own completion even if the move batch
            # later needs replaying.
            self._commit(f"archive:{work_id}:finalize", writes=[
                (path / "work.yaml", self._yaml_text(work)),
                (path / "summary.md", summary),
            ])
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Batch 2: per-file moves of every artifact plus the index update
            # as ONE journal — a crash mid-archive leaves a pending journal
            # that `recover`/`repair` replays to finish the moves and the
            # index deregistration together. Directories are never moved:
            # their empty contents remain staged under active/ (no data
            # deletion), harmless because the index no longer references the
            # work.
            files = sorted((item for item in path.rglob("*") if item.is_file()),
                           key=lambda item: item.relative_to(path).as_posix())
            index = self._validate_index()
            index["active"] = [x for x in index["active"] if x["id"] != work_id]
            if index.get("focused") == work_id:
                index["focused"] = None
            self._commit(f"archive:{work_id}:move",
                         moves=[(item, dest / item.relative_to(path)) for item in files],
                         writes=[(self.index_path, self._yaml_text(index))])
            # Journalable ops are files only; recreate the directory layout at
            # the destination as plain metadata so the archive keeps the
            # original shape. Empty source dirs stay in place under active/
            # (staged), never deleted.
            for directory in {item for item in path.rglob("*") if item.is_dir()}:
                (dest / directory.relative_to(path)).mkdir(parents=True, exist_ok=True)
            append_event(self.root, "work_archived", work=work_id, path=str(dest.relative_to(self.root)))
            return dest

    def _summary(self, work: dict[str, Any], tasks: list[dict[str, Any]], path: Path) -> str:
        requirements = "\n".join(f"- {r['id']}: {r.get('status', 'unknown')} — {r.get('description', '')}" for r in work.get("requirements", [])) or "- No structured requirements recorded."
        decisions = ((path / "decisions.md").read_text(encoding="utf-8") if (path / "decisions.md").exists()
                     else "\n".join(f"- {d['id']}: {d['text']} — {d['rationale']}" for d in work.get("decisions", [])))
        request = (path / "request.md").read_text(encoding="utf-8")
        objective = request.split("## User clarifications and changes", 1)[0].replace("# Original user request", "", 1).strip()
        objective = objective[:1000] + ("…" if len(objective) > 1000 else "")
        if work["status"] == "cancelled":
            return f"# {work['title']}\n\nStatus: cancelled\n\n## Original request\n\n{objective}\n\nNo completion or verification is implied.\n"
        changed = sorted({f for t in tasks for f in self._result(path, t).get("changed", [])})
        checks = sorted({v for t in tasks for v in self._verdict(path, t).get("checks", [])})
        return (f"# {work['title']}\n\n## Objective\n\n{objective}\n\n## Result\n\nCompleted and independently verified {len(tasks)} task(s).\n\n## Requirements\n\n{requirements}\n\n## Decisions\n\n{decisions}\n\n## Changed files\n\n" + "\n".join(f"- `{x}`" for x in changed) + "\n\n## Independent verification\n\n" + "\n".join(f"- {x}" for x in checks) + f"\n\nHEAD at close (if changed since start): `{work.get('final_commit') or 'not recorded'}`\n")

    def _git_metadata(self) -> dict[str, str | None]:
        def git(*args: str) -> str | None:
            try:
                return subprocess.check_output(["git", *args], cwd=self.project, stderr=subprocess.DEVNULL, text=True,
                                               timeout=10).strip() or None
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return None
        return {"commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current")}

    def _approval_hash(self, path: Path, tasks: list[dict[str, Any]] | None = None) -> str:
        digest = hashlib.sha256()
        files = [path / "request.md"] + [p for name in ("spec.md", "plan.yaml") if (p := path / name).exists()]
        for file in files:
            digest.update(file.relative_to(path).as_posix().encode())
            digest.update(b"\0")
            content = file.read_bytes()
            if file.name == "spec.md":
                content = re.sub(rb"(?m)^Status: .*?$", b"Status: <mutable>", content)
            digest.update(content)
            digest.update(b"\0")
        if tasks is None:
            # Reuses the caller's loaded snapshot where available instead of
            # revalidating every packet on each hash computation.
            tasks = self.load_tasks(path)
        for task in tasks:
            contract = {key: value for key, value in task.items() if key not in {"status", "attempts", "verified_at", "result", "verdict", "started_contract_hash"}}
            digest.update(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode())
            digest.update(b"\0")
        work = read_yaml(path / "work.yaml")
        requirements = [{**{key: value for key, value in item.items() if key != "status"},
                         "status": "waived" if item.get("status") == "waived" else "<mutable>"}
                        for item in work.get("requirements", [])]
        digest.update(json.dumps(requirements, sort_keys=True, ensure_ascii=False).encode())
        digest.update(json.dumps(work.get("decisions", []), sort_keys=True, ensure_ascii=False).encode())
        return digest.hexdigest()

    def _task_contract_hash(self, path: Path, task: dict[str, Any], work: dict[str, Any] | None = None) -> str:
        if work is None:
            work = self.load_work(path)
        requirements = [req for req in work["requirements"] if req["id"] in task["satisfies"]]
        return contracts.task_contract_hash(task, requirements)

    def _assert_task_contract(self, path: Path, task: dict[str, Any], work: dict[str, Any] | None = None) -> None:
        if task.get("started_contract_hash") and task["started_contract_hash"] != self._task_contract_hash(path, task, work=work):
            raise ValidationError(f"Task {task['id']} contract changed since execution; invalidate and rerun it")

    def approve_work(self, work_id: str, note: str) -> None:
        if not note.strip():
            raise ValidationError("Approval requires a note documenting the user's approval")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            work = read_yaml(path / "work.yaml")
            if work["status"] != "awaiting_approval":
                raise ValidationError(f"Cannot approve a plan in work state {work['status']}")
            tasks = self.load_tasks(path)
            if any(task["status"] in {"running", "review"} for task in tasks):
                raise ValidationError("Cannot reapprove while tasks are running or awaiting review; reconcile them first")
            self._validate_contract(path, work, tasks=tasks)
            record = {"approved": True, "approved_at": datetime.now(timezone.utc).isoformat(),
                      "approval_note": note.strip(), "artifact_hash": self._approval_hash(path, tasks)}
            if (path / "plan.yaml").exists():
                write_yaml(path / "approval.yaml", record)
            else:
                work["approval"] = {key: record[key] for key in ("approved_at", "approval_note", "artifact_hash")}
            if (path / "plan.yaml").exists():
                work["plan_approved"] = True
            work["status"] = "executing"
            work["updated_at"] = record["approved_at"]
            self._schema_validate("work", work)
            write_yaml(path / "work.yaml", work)
            append_event(self.root, "plan_approved", work=work_id)

    def _assert_plan_approved(self, work_id: str, path: Path, tasks: list[dict[str, Any]] | None = None,
                              config: dict[str, Any] | None = None) -> None:
        if config is None:
            config = self.load_config()
        if not config.get("approval", {}).get("plan", True):
            return
        work = read_yaml(path / "work.yaml")
        approval = work.get("approval") or read_yaml(path / "approval.yaml", {})
        if not work.get("approval") and (not work.get("plan_approved") or approval.get("approved") is not True):
            raise ValidationError("Plan requires explicit approval before task execution")
        if approval.get("artifact_hash") != self._approval_hash(path, tasks):
            raise ValidationError("Approved spec/plan/tasks changed; review and approve the revised plan again")
