"""Durable work artifact manager."""
from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
from pathlib import PurePosixPath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .domain import invalidate_closure, ready_tasks, scope_matches, transition, validate_dag
from .errors import ConflictError, ValidationError
from .fs import append_event, atomic_write, file_lock, read_yaml, write_yaml


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
            self.project / ".dado" / "config.yaml": "approval:\n  plan: true\nscheduler:\n  max_parallel_workers: 3\nverification:\n  default_risk: medium\nhistory:\n  automatic_search: false\narchive:\n  completed: true\n",
            self.root / "project" / "architecture.md": "# Project architecture\n\nPromote durable, sourced project facts here.\n",
            self.root / "project" / "conventions.md": "# Project conventions\n\nPromote verified conventions here.\n",
            self.root / "project" / "constraints.md": "# Project constraints\n\nRecord durable constraints with provenance.\n",
            self.root / "project" / "decisions.md": "# Project decisions\n\nDurable decisions only; include source work IDs.\n",
        }
        for path, content in defaults.items():
            if not path.exists():
                atomic_write(path, content)
        if not self.index_path.exists():
            write_yaml(self.index_path, {"focused": None, "active": []})
        self._validate_index()
        self.load_config()

    def load_config(self) -> dict[str, Any]:
        path = self.project / ".dado" / "config.yaml"
        if not path.exists():
            raise ValidationError("DADO config is missing; run `dado init` to restore defaults")
        config = read_yaml(path)
        self._schema_validate("config", config)
        return config

    def _schema_validate(self, kind: str, data: Any) -> None:
        schema_path = self.schemas / f"{kind}.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
        if errors:
            raise ValidationError(f"Invalid {kind}: " + "; ".join(e.message for e in errors[:5]))

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
            write_yaml(path / "work.yaml", work)
            atomic_write(path / "request.md", f"# Original user request\n\n{request.rstrip()}\n\n## User clarifications and changes\n\n<!-- Append dated user-provided changes. Do not rewrite original request. -->\n")
            index = self._validate_index()
            index["active"].append({"id": work_id, "path": f"active/{work_id}"})
            index["focused"] = work_id
            self._schema_validate("index", index)
            write_yaml(self.index_path, index)
            append_event(self.root, "work_created", work=work_id)
        return path

    def list_active(self) -> list[dict[str, Any]]:
        items = []
        for item in self._validate_index()["active"]:
            work = self.load_work(self.work_path(item["id"]))
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

    def work_path(self, work_id: str) -> Path:
        item = next((x for x in self._validate_index()["active"] if x["id"] == work_id), None)
        if not item:
            raise ValidationError(f"No active work {work_id}; archives are cold storage")
        path = (self.root / item["path"]).resolve()
        if not path.is_relative_to(self.active.resolve()):
            raise ValidationError("Work path escapes active directory")
        return path

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
        if (path / "plan.yaml").exists():
            plan = read_yaml(path / "plan.yaml")
            self._schema_validate("plan", plan)
            graph = plan["tasks"]
            expected = {item["id"]: item["depends_on"] for item in graph}
            if len(expected) != len(graph) or expected != {t.get("id"): t.get("depends_on", []) for t in tasks}:
                raise ValidationError("Task packets and plan DAG disagree; preserve artifacts and reconcile manually")
        validate_dag(tasks)
        for task in tasks:
            self._schema_validate("task", task)
            if "result" in task:
                self._schema_validate("result", task["result"])
            if "verdict" in task:
                self._schema_validate("verification", task["verdict"])
            self._validate_task_scope(task)
        return tasks

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
            self._schema_validate("work", work)
            if (path / "spec.md").exists():
                spec = path / "spec.md"
                body = "\n".join(f"- {criterion}" for criterion in requirement["acceptance"])
                section = (f"\n## {requirement['id']} — {requirement['description']}\n\n"
                           f"Source: {requirement['source']}\n\nStatus: {requirement['status']}\n\nAcceptance:\n{body}\n")
                atomic_write(spec, spec.read_text(encoding="utf-8").rstrip() + "\n" + section)
            write_yaml(path / "work.yaml", work)
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
            write_yaml(path / "work.yaml", work)
            if status == "waived" and (path / "decisions.md").exists():
                decisions = path / "decisions.md"
                content = decisions.read_text(encoding="utf-8")
                numbers = [int(n) for n in re.findall(r"^## DEC-(\d{3,})\b", content, re.M)]
                decision_id = f"DEC-{max(numbers, default=0) + 1:03d}"
                block = f"\n## {decision_id} — Waive {requirement_id}\n\nDate: {datetime.now(timezone.utc).date().isoformat()}\n\nRationale: {reason.strip()}\n\nRequirements: {requirement_id}\n\nTasks: none\n"
                atomic_write(decisions, content.rstrip() + "\n" + block)
            if (path / "spec.md").exists():
                spec = path / "spec.md"
                content = spec.read_text(encoding="utf-8")
                marker = f"## {requirement_id} "
                start = content.find(marker)
                if start >= 0:
                    next_section = content.find("\n## ", start + len(marker))
                    end = next_section if next_section >= 0 else len(content)
                    section = content[start:end]
                    section = re.sub(r"(?m)^Status: .*?$", f"Status: {status}", section)
                    atomic_write(spec, content)
            append_event(self.root, "requirement_status_changed", work=work_id, requirement=requirement_id, status=status)

    def _validate_contract(self, path: Path, work: dict[str, Any]) -> None:
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

    def save_tasks(self, path: Path, tasks: list[dict[str, Any]]) -> None:
        validate_dag(tasks)
        for task in tasks:
            self._schema_validate("task", task)
        # Task packets are the graph; old work keeps its legacy plan index.
        for task in tasks:
            write_yaml(path / "tasks" / f"{task['id']}.yaml", task)
        if (path / "plan.yaml").exists():
            graph = [{"id": t["id"], "depends_on": t.get("depends_on", [])} for t in tasks]
            plan = {"version": 1, "tasks": graph}
            self._schema_validate("plan", plan)
            write_yaml(path / "plan.yaml", plan)

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
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task is None:
                raise ValidationError(f"Unknown task {task_id}")
            work = self.load_work(path)
            if work["status"] in {"cancelled", "completed"} and state not in {"blocked", "failed", "cancelled", "stale"}:
                raise ValidationError(f"Cannot advance a task in terminal work state {work['status']}")
            if state == "done":
                raise ValidationError("A task can become done only through an independent verifier result")
            if state == "running":
                self._assert_plan_approved(work_id, path)
                if task not in ready_tasks(tasks, self.load_config().get("scheduler", {}).get("max_parallel_workers", 3)):
                    raise ValidationError(f"{task_id} is not runnable: capacity or scope conflict")
            if state == "review":
                worker_result = self._result(path, task)
                if worker_result.get("status") != "completed":
                    raise ValidationError("A completed worker Result Packet must exist before independent review")
            if state in {"ready", "running", "review", "done"} and any(next(x for x in tasks if x["id"] == d)["status"] != "done" for d in task.get("depends_on", [])):
                raise ValidationError(f"Dependencies are not complete for {task_id}")
            previous = task["status"]
            transition(task, state)
            if state == "running":
                task["attempts"] += 1
                task.pop("result", None)
                task.pop("verdict", None)
                task["started_contract_hash"] = self._task_contract_hash(path, task)
                work = read_yaml(path / "work.yaml")
                if work["status"] in {"completed", "cancelled"}:
                    raise ValidationError(f"Cannot start a task in terminal work state {work['status']}")
                if work["status"] != "executing":
                    work["status"] = "executing"
                    work["updated_at"] = datetime.now(timezone.utc).isoformat()
                    write_yaml(path / "work.yaml", work)
                if (path / "plan.yaml").exists():
                    # Preserve superseded legacy packets without mistaking them for this run after a crash.
                    old_result = path / "results" / f"{task_id}.yaml"
                    old_verdict = path / "verification" / f"{task_id}.yaml"
                    if old_result.exists() or old_verdict.exists():
                        write_yaml(path / "evidence" / "attempts" / f"{task_id}-{task['attempts'] - 1:03d}.yaml",
                                   {"result": read_yaml(old_result), "verdict": read_yaml(old_verdict)})
                    old_result.unlink(missing_ok=True)
                    old_verdict.unlink(missing_ok=True)
                append_event(self.root, "task_started", work=work_id, task=task_id, attempt=task["attempts"])
            self.save_tasks(path, tasks)
            append_event(self.root, "task_state_changed", work=work_id, task=task_id, previous=previous, status=state)

    def verify_task(self, work_id: str, task_id: str, verdict: str, checks: list[str], evidence: list[str] | None = None,
                    checked_paths: list[str] | None = None) -> None:
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
            raise ValidationError("Verifier verdict must be PASS, FAIL, or BLOCKED")
        for ref in evidence or []:
            ref_path = PurePosixPath(ref.replace("\\", "/"))
            if ref_path.is_absolute() or ".." in ref_path.parts or not ref_path.parts or ref_path.parts[0] != "evidence":
                raise ValidationError(f"Verifier evidence references must be relative to this work's evidence/: {ref}")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            task = next((t for t in tasks if t["id"] == task_id), None)
            if not task or task["status"] != "review":
                raise ValidationError(f"{task_id} must be in review before independent verification")
            if self.load_work(path)["status"] in {"cancelled", "completed"}:
                raise ValidationError("Cannot verify a task in a terminal work state")
            if verdict == "PASS":
                self._assert_plan_approved(work_id, path)
                self._assert_task_contract(path, task)
                if checked_paths is None:
                    raise ValidationError("PASS requires the verifier's independently checked paths (an empty list means no changes)")
                if sorted(set(checked_paths)) != sorted(set(self._result(path, task).get("changed", []))):
                    raise ValidationError("Verifier checked paths disagree with the worker result")
                self._check_scope(task, checked_paths)
            record = {"task": task_id, "verdict": verdict, "checks": checks, "evidence": evidence or [],
                      "verified_at": datetime.now(timezone.utc).isoformat()}
            if checked_paths is not None:
                record["checked_paths"] = checked_paths
            self._schema_validate("verification", record)
            if (path / "plan.yaml").exists():
                write_yaml(path / "verification" / f"{task_id}.yaml", record)
            else:
                task["verdict"] = record
            if verdict == "PASS":
                task["status"] = "done"
                task["verified_at"] = record["verified_at"]
                append_event(self.root, "verification_passed", work=work_id, task=task_id)
                append_event(self.root, "task_completed", work=work_id, task=task_id)
            else:
                task["status"] = "blocked" if verdict == "BLOCKED" else "failed"
                append_event(self.root, "verification_failed", work=work_id, task=task_id, verdict=verdict)
            self.save_tasks(path, tasks)

    def record_result(self, work_id: str, result: dict[str, Any]) -> None:
        path = self.work_path(work_id)
        self._schema_validate("result", result)
        for ref in result.get("evidence", []):
            ref_path = PurePosixPath(ref.replace("\\", "/"))
            if ref_path.is_absolute() or ".." in ref_path.parts or not ref_path.parts or ref_path.parts[0] != "evidence":
                raise ValidationError(f"Evidence references must be relative to this work's evidence/: {ref}")
        with file_lock(self.root / ".state"):
            tasks = self.load_tasks(path)
            task = next((t for t in tasks if t["id"] == result["task"]), None)
            if not task:
                raise ValidationError(f"Unknown task {result['task']}")
            if task["status"] != "running":
                raise ValidationError(f"Cannot record worker result while {task['id']} is {task['status']}; expected running")
            if self.load_work(path)["status"] in {"cancelled", "completed"}:
                raise ValidationError("Cannot record a result in a terminal work state")
            self._assert_plan_approved(work_id, path)
            self._assert_task_contract(path, task)
            self._check_scope(task, result.get("changed", []))
            if (path / "plan.yaml").exists():
                write_yaml(path / "results" / f"{result['task']}.yaml", result)
            else:
                task["result"] = result
            target = "review" if result["status"] == "completed" else "blocked" if result["status"] == "blocked" else "failed"
            transition(task, target)
            self.save_tasks(path, tasks)
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

    def ready(self, work_id: str, max_parallel: int = 3) -> list[dict[str, Any]]:
        path = self.work_path(work_id)
        if self.load_work(path)["status"] in {"cancelled", "completed"}:
            return []
        return ready_tasks(self.load_tasks(path), max_parallel)

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
                self._assert_plan_approved(work_id, path)
                self._validate_contract(path, work)
                for task in tasks:
                    self._assert_task_contract(path, task)
                if not tasks or any(t["status"] != "done" for t in tasks):
                    raise ValidationError("Work cannot be completed until all tasks are done")
                if any(self._verdict(path, t).get("verdict") != "PASS" for t in tasks):
                    raise ValidationError("Work cannot be completed without independent PASS evidence")
                if any(r["status"] not in {"done", "waived"} for r in work["requirements"]):
                    raise ValidationError("Work cannot be completed until requirements are done or explicitly waived")
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
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            if self.load_work(path)["status"] in {"cancelled", "completed"}:
                return []
            tasks = self.load_tasks(path)
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
                        task["status"] = "ready" if all(next(t for t in tasks if t["id"] == d)["status"] == "done" for d in task.get("depends_on", [])) else "pending"
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
                self._assert_plan_approved(work_id, path)
                self._validate_contract(path, work)
                for task in tasks:
                    self._assert_task_contract(path, task)
                if not tasks or any(t["status"] != "done" for t in tasks):
                    raise ValidationError("Cannot archive: every task must be independently verified and done")
                for task in tasks:
                    verification = self._verdict(path, task)
                    if verification.get("verdict") != "PASS":
                        raise ValidationError(f"Cannot archive: {task['id']} lacks independent PASS evidence")
                    self._schema_validate("verification", verification)
                if any(r.get("status") not in {"done", "waived"} for r in work.get("requirements", [])):
                    raise ValidationError("Cannot archive: all requirements must be done or explicitly waived")
                work["status"] = "completed"
            elif any(t["status"] in {"running", "review"} for t in tasks):
                raise ValidationError("Cannot archive cancelled work with running or reviewing tasks; reconcile them first")
            work["updated_at"] = datetime.now(timezone.utc).isoformat()
            if work["status"] == "completed":
                head = self._git_metadata().get("commit")
                work["final_commit"] = head if head != work.get("base_commit") else None
            write_yaml(path / "work.yaml", work)
            summary = self._summary(work, tasks, path)
            atomic_write(path / "summary.md", summary)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            index = self._validate_index()
            index["active"] = [x for x in index["active"] if x["id"] != work_id]
            if index.get("focused") == work_id:
                index["focused"] = None
            write_yaml(self.index_path, index)
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
                return subprocess.check_output(["git", *args], cwd=self.project, stderr=subprocess.DEVNULL, text=True).strip() or None
            except (OSError, subprocess.CalledProcessError):
                return None
        return {"commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current")}

    def _approval_hash(self, path: Path) -> str:
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
        for task in self.load_tasks(path):
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

    def _task_contract_hash(self, path: Path, task: dict[str, Any]) -> str:
        contract = {key: value for key, value in task.items()
                    if key not in {"status", "attempts", "verified_at", "result", "verdict", "started_contract_hash"}}
        work = self.load_work(path)
        requirements = [{key: value for key, value in req.items() if key != "status"}
                        for req in work["requirements"] if req["id"] in task["satisfies"]]
        payload = {"task": contract, "requirements": requirements}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _assert_task_contract(self, path: Path, task: dict[str, Any]) -> None:
        if task.get("started_contract_hash") and task["started_contract_hash"] != self._task_contract_hash(path, task):
            raise ValidationError(f"Task {task['id']} contract changed since execution; invalidate and rerun it")

    def approve_work(self, work_id: str, note: str) -> None:
        if not note.strip():
            raise ValidationError("Approval requires a note documenting the user's approval")
        path = self.work_path(work_id)
        with file_lock(self.root / ".state"):
            work = read_yaml(path / "work.yaml")
            if work["status"] != "awaiting_approval":
                raise ValidationError(f"Cannot approve a plan in work state {work['status']}")
            if any(task["status"] in {"running", "review"} for task in self.load_tasks(path)):
                raise ValidationError("Cannot reapprove while tasks are running or awaiting review; reconcile them first")
            self._validate_contract(path, work)
            record = {"approved": True, "approved_at": datetime.now(timezone.utc).isoformat(),
                      "approval_note": note.strip(), "artifact_hash": self._approval_hash(path)}
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

    def _assert_plan_approved(self, work_id: str, path: Path) -> None:
        if not self.load_config().get("approval", {}).get("plan", True):
            return
        work = read_yaml(path / "work.yaml")
        approval = work.get("approval") or read_yaml(path / "approval.yaml", {})
        if not work.get("approval") and (not work.get("plan_approved") or approval.get("approved") is not True):
            raise ValidationError("Plan requires explicit approval before task execution")
        if approval.get("artifact_hash") != self._approval_hash(path):
            raise ValidationError("Approved spec/plan/tasks changed; review and approve the revised plan again")
