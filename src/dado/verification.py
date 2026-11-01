"""Code-bound acceptance rules shared by task and work lifecycle operations."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ValidationError
from .evidence import RECORD_KIND, load_record, snapshot, verify_record
from .fs import atomic_write


def snapshot_scopes(project: Path, scopes: list[str]) -> list[str] | None:
    """Bind globs and absent targets to existing ancestors, never current matches.

    This conservative scope also detects files added after PASS. Expanding a
    glob to today's matches would silently omit tomorrow's matching files.
    """
    roots = set()
    for scope in scopes:
        parts = []
        for part in PurePosixPath(scope.replace("\\", "/")).parts:
            if part == "..":
                raise ValidationError("Verification scope cannot escape the project")
            if any(char in part for char in "*?["):
                break
            parts.append(part)
        candidate = project.joinpath(*parts)
        while not candidate.exists() and candidate != project:
            candidate = candidate.parent
        if not candidate.resolve().is_relative_to(project.resolve()):
            raise ValidationError("Verification scope cannot escape the project")
        relative = candidate.relative_to(project).as_posix()
        if relative == ".":
            return None
        roots.add(relative)
    return sorted(roots) or None


def compact_snapshot(project: Path, scopes: list[str] | None = None) -> dict[str, Any]:
    value = snapshot(project, scopes)
    return {key: value[key] for key in ("version", "digest", "scopes")}


def assert_snapshot(project: Path, recorded: dict) -> None:
    if not isinstance(recorded, dict) or set(recorded) != {"version", "digest", "scopes"}:
        raise ValidationError("Invalid recorded verification snapshot")
    if compact_snapshot(project, recorded["scopes"]) != recorded:
        raise ValidationError("Code changed since verification; rerun checks and independent verification")


def evidence_file(work_path: Path, reference: str) -> Path:
    parts = PurePosixPath(reference.replace("\\", "/")).parts
    if not parts or parts[0] != "evidence" or ".." in parts or ":" in reference:
        raise ValidationError("Captured evidence must be a relative path inside this work's evidence/")
    path = work_path.joinpath(*parts)
    if not path.resolve().is_relative_to((work_path / "evidence").resolve()) or not path.resolve().is_relative_to(work_path.resolve()):
        raise ValidationError("Captured evidence path escapes this work")
    return path


def captured_coverage(project: Path, work_path: Path, references: list[str], identity: str, criteria: list[str]) -> list[str]:
    covered: set[str] = set()
    for ref in references:
        path = evidence_file(work_path, ref)
        record = load_record(path)
        if not isinstance(record, dict) or record.get("kind") != RECORD_KIND:
            raise ValidationError(f"Not an executed check record: {ref}")
        if Path(record.get("evidence_dir", "")).resolve() != path.parent.resolve() or Path(record.get("project", "")).resolve() != project.resolve():
            raise ValidationError("Check record provenance does not match this project/evidence directory")
        verify_record(record, project=project)
        observed = record["observed"]
        if observed.get("timed_out") or observed.get("exit_code") != 0 or record["result"].get("criterion_met") is not True:
            raise ValidationError(f"Captured check did not succeed: {ref}")
        declaration = record["declared"].get("criterion", {})
        declared = declaration.get("acceptance", [])
        if declaration.get("subject") != identity or not isinstance(declared, list) or any(not isinstance(x, str) or x not in criteria for x in declared):
            raise ValidationError("Captured check does not map to this contract's acceptance criteria")
        covered.update(declared)
    missing = set(criteria) - covered
    if missing:
        raise ValidationError("Missing captured acceptance checks: " + "; ".join(sorted(missing)))
    return sorted(covered)


def task_evidence(store, path: Path, task: dict, references: list[str] | None = None) -> dict:
    refs = references or []
    required = store.load_config().get("verification", {}).get("require_captured", False)
    if refs or required:
        captured_coverage(store.project, path, refs, task["id"], task["acceptance"])
    return {"code_snapshot": compact_snapshot(store.project, snapshot_scopes(store.project, task["scope"]["allowed"])),
            "evidence_mode": "captured" if refs else "declared", "criteria": list(task["acceptance"]),
            "captured_records": refs}


def assert_task_evidence(store, path: Path, task: dict) -> str:
    verdict = store._verdict(path, task)
    recorded = verdict.get("code_snapshot")
    if recorded is None:
        if store.load_config().get("verification", {}).get("require_captured", False):
            raise ValidationError("Legacy verification does not satisfy the current captured-evidence policy")
        return "legacy-declared"
    assert_snapshot(store.project, recorded)
    refs = verdict.get("captured_records", [])
    required = store.load_config().get("verification", {}).get("require_captured", False)
    if refs or required:
        captured_coverage(store.project, path, refs, task["id"], task["acceptance"])
    return verdict.get("evidence_mode", "declared")


def integration_criteria(work: dict) -> list[str]:
    return [f"{requirement['id']}: {item}" for requirement in work["requirements"]
            if requirement["status"] != "waived" for item in requirement["acceptance"]]


def record_integration(store, path: Path, references: list[str]) -> dict:
    work = store.load_work(path)
    tasks = store.load_tasks(path)
    if not tasks or any(task["status"] != "done" for task in tasks):
        raise ValidationError("Integration verification requires all tasks to be done")
    store._assert_plan_approved(work["id"], path)
    for task in tasks:
        assert_task_evidence(store, path, task)
    criteria = integration_criteria(work)
    captured_coverage(store.project, path, references, work["id"], criteria)
    record = {"version": 1, "work": work["id"], "code_snapshot": compact_snapshot(store.project),
              "captured_records": references, "criteria": criteria,
              "contract_hash": store._approval_hash(path), "verdict": "PASS"}
    atomic_write(path / "integration.json", json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


def assert_integration(store, path: Path) -> None:
    target = path / "integration.json"
    required = store.load_config().get("verification", {}).get("require_integration", False)
    if not target.exists():
        if required:
            raise ValidationError("Work requires captured integration verification before completion/archive")
        return
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError("Invalid integration verification record") from exc
    work = store.load_work(path)
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("work") != work["id"] or record.get("verdict") != "PASS" or record.get("contract_hash") != store._approval_hash(path):
        raise ValidationError("Integration verification does not match the current contract")
    assert_snapshot(store.project, record.get("code_snapshot"))
    captured_coverage(store.project, path, record.get("captured_records", []), work["id"], integration_criteria(work))
