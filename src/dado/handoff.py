"""Explicit portable work exchange, never a distributed task reservation.

Bundles contain selected contracts and evidence, not config, profiles, sessions
or machine paths. Imported verdicts are history: tasks always require rerun and
fresh approval. Review free-text contracts before sharing confidential work.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import uuid

import yaml

from .domain import validate_dag
from .errors import ConflictError, ValidationError
from .fs import atomic_write, file_lock, read_yaml, write_yaml
from .store import Store

MAX_BUNDLE = 16 * 1024 * 1024
RUNTIME_FIELDS = {"status", "attempts", "verified_at", "started_contract_hash", "result", "verdict"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _portable_evidence(data: bytes) -> bytes:
    """Captured records become historical summaries without machine/session paths."""
    try:
        record = json.loads(data)
    except (ValueError, UnicodeError):
        return data
    if not isinstance(record, dict) or record.get("kind") != "dado/evidence/check-record":
        return data
    observed = record.get("observed", {})
    portable = {"kind": "dado/handoff/check-summary", "version": 1, "verification": "historical-only",
                "origin_record_hash": record.get("integrity", {}).get("record_sha256"),
                "criterion": record.get("declared", {}).get("criterion"),
                "exit_code": observed.get("exit_code"), "timed_out": observed.get("timed_out"),
                "duration_seconds": observed.get("duration_seconds"),
                "code_digest": observed.get("before_snapshot", {}).get("digest"),
                "result": record.get("result"), "logs": observed.get("logs", [])}
    return json.dumps(portable, ensure_ascii=False, sort_keys=True).encode()


def _evidence_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise ValidationError("Handoff evidence must use relative POSIX paths")
    parts = PurePosixPath(value).parts
    if not parts or parts[0] != "evidence" or ".." in parts or PurePosixPath(value).is_absolute():
        raise ValidationError("Handoff evidence must be under evidence/")
    if any(part.startswith(".env") or part.lower().endswith((".pem", ".key")) for part in parts):
        raise ValidationError("Secret files cannot be included in a handoff")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValidationError("Handoff evidence cannot traverse symlinks")
    return path


def export_work(store: Store, work_id: str, destination: Path, evidence: list[str] | None = None) -> dict:
    """Explicitly export an active work, with only named evidence files."""
    with file_lock(store.root / ".state"):
        path = store.work_path(work_id)
        work = store.load_work(path)
        tasks = store.load_tasks(path)
        selected = {}
        total = 0
        for name in evidence or []:
            item = _evidence_path(path, name)
            if not item.is_file() or item.stat().st_size > MAX_BUNDLE:
                raise ValidationError(f"Missing or oversized evidence: {name}")
            data = _portable_evidence(item.read_bytes())
            total += len(data)
            if total > MAX_BUNDLE // 2:
                raise ValidationError("Selected evidence exceeds handoff size limit")
            selected[name] = {"sha256": hashlib.sha256(data).hexdigest(), "base64": base64.b64encode(data).decode()}
        contracts = []
        history = []
        for task in tasks:
            contract = {key: value for key, value in task.items() if key not in RUNTIME_FIELDS}
            # Context references can contain absolute machine paths. Portable
            # contracts may be enriched by the recipient after explicit review.
            contract["context_refs"] = [ref for ref in contract.get("context_refs", [])
                                        if not Path(ref).is_absolute() and not re.match(r"^[A-Za-z]:", ref)]
            contracts.append(contract)
            history.append({"task": task["id"], "status": task["status"],
                            "result": store._result(path, task), "verdict": store._verdict(path, task)})
        payload = {"version": 1, "origin_work": work["id"], "title": work["title"],
                   "base_commit": work.get("base_commit"), "request": (path / "request.md").read_text(encoding="utf-8"),
                   "requirements": work["requirements"], "tasks": contracts,
                   "history": history, "evidence": selected}
        identity = read_yaml(path / "identity.yaml", {})
        if not isinstance(identity, dict):
            raise ValidationError("Invalid work identity artifact")
        payload["origin"] = {"project_uuid": identity.get("project_uuid"),
                             "work_uuid": identity.get("work_uuid"), "created_at": work["created_at"]}
        envelope = {"sha256": _digest(payload), "payload": payload}
        text = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(text.encode()) > MAX_BUNDLE:
            raise ValidationError("Handoff bundle exceeds size limit")
        if destination.exists():
            raise ConflictError(f"Handoff destination already exists: {destination}")
        atomic_write(destination, text + "\n")
    return {"bundle": str(destination), "sha256": envelope["sha256"], "requires_review_before_sharing": True}


def load_bundle(source: Path, store: Store) -> tuple[str, dict]:
    if source.stat().st_size > MAX_BUNDLE:
        raise ValidationError("Handoff bundle exceeds size limit")
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
        payload = envelope["payload"]
        digest = envelope["sha256"]
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValidationError("Invalid handoff JSON envelope") from exc
    if not isinstance(payload, dict) or digest != _digest(payload) or payload.get("version") != 1:
        raise ValidationError("Unsupported or corrupt handoff bundle")
    required = {"version", "origin_work", "title", "base_commit", "request", "requirements", "tasks", "history", "evidence"}
    if set(payload) not in (required, required | {"origin"}) or not all(isinstance(payload[k], str) for k in ("origin_work", "title", "request")):
        raise ValidationError("Invalid handoff contract fields")
    if "origin" in payload:
        origin = payload["origin"]
        if not isinstance(origin, dict) or set(origin) != {"project_uuid", "work_uuid", "created_at"} or any(value is not None and not isinstance(value, str) for value in origin.values()):
            raise ValidationError("Invalid portable origin identity")
    if not isinstance(payload["tasks"], list) or not isinstance(payload["requirements"], list) or not isinstance(payload["evidence"], dict) or not isinstance(payload["history"], list):
        raise ValidationError("Invalid handoff task, requirement or evidence collection")
    tasks = []
    for contract in payload["tasks"]:
        if not isinstance(contract, dict) or set(contract) & RUNTIME_FIELDS:
            raise ValidationError("Handoff task contract contains execution state")
        task = {**contract, "status": "pending", "attempts": 0}
        store._schema_validate("task", task)
        store._validate_task_scope(task)
        tasks.append(task)
    validate_dag(tasks)
    # Validate requirements through the canonical work schema before mutations.
    store._schema_validate("work", {"id": payload["origin_work"], "title": payload["title"],
        "status": "discovery", "created_at": "", "updated_at": "", "requirements": payload["requirements"], "base_commit": payload["base_commit"]})
    ids = [requirement["id"] for requirement in payload["requirements"]]
    if len(ids) != len(set(ids)) or any(set(task["satisfies"]) - set(ids) for task in tasks):
        raise ValidationError("Handoff contains duplicate or unknown requirements")
    task_ids = {task["id"] for task in tasks}
    for item in payload["history"]:
        if not isinstance(item, dict) or item.get("task") not in task_ids:
            raise ValidationError("Handoff history must refer to known tasks")
        for field, schema in (("result", "result"), ("verdict", "verification")):
            if item.get(field):
                store._schema_validate(schema, item[field])
    for name, record in payload["evidence"].items():
        _evidence_path(store.root / "import-validation", name)
        try:
            data = base64.b64decode(record["base64"], validate=True)
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("hash mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError(f"Invalid handoff evidence {name}") from exc
    return digest, payload


def import_work(store: Store, source: Path) -> Path:
    """Import into a new local execution; deterministic ID rejects duplicates."""
    digest, payload = load_bundle(source, store)
    work_id = "WORK-import-" + digest[:24]
    store.init()
    if (store.active / work_id).exists() or list((store.root / "archive").glob(f"*/{work_id}")):
        raise ConflictError(f"Handoff was already imported as {work_id}")
    # A staged operation is integrated below using the transaction journal.
    return _install_import(store, work_id, digest, payload)


def _install_import(store: Store, work_id: str, digest: str, payload: dict) -> Path:
    # This entry point deliberately resets all execution and approval state.
    # Integration uses a private staging directory so partial validation never
    # publishes an apparently ready task into the active index.
    from datetime import datetime, timezone
    staged = store.root / "imports" / (work_id + "-" + uuid.uuid4().hex)
    staged.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    requirements = [{**r, "status": "pending"} for r in payload["requirements"]]
    work = {"id": work_id, "title": payload["title"], "status": "discovery", "created_at": now,
            "updated_at": now, "base_commit": store._git_metadata().get("commit"),
            "branch": store._git_metadata().get("branch"), "final_commit": None,
            "requirements": requirements, "decisions": []}
    store._schema_validate("work", work)
    write_yaml(staged / "work.yaml", work)
    from .layout import is_layout2, project_uuid, checkout_uuid
    write_yaml(staged / "identity.yaml", {"version": 1, "work_uuid": str(uuid.uuid4()),
        "execution_uuid": str(uuid.uuid4()), "project_uuid": project_uuid(store.project) if is_layout2(store.project) else None,
        "checkout_uuid": checkout_uuid(store.project) if is_layout2(store.project) else None,
        "origin": payload.get("origin", {}), "origin_bundle": digest})
    atomic_write(staged / "request.md", payload["request"] + f"\n\nImported from {payload['origin_work']} ({digest}); new approval and execution required.\n")
    tasks = [{**copy.deepcopy(task), "status": "pending", "attempts": 0} for task in payload["tasks"]]
    store.save_tasks(staged, tasks)
    atomic_write(staged / "handoff.json", json.dumps({"version": 1, "origin_work": payload["origin_work"],
        "origin_base_commit": payload["base_commit"], "sha256": digest, "history": payload["history"],
        "execution_id": uuid.uuid4().hex, "verification": "historical-only"}, ensure_ascii=False, indent=2))
    for name, record in payload["evidence"].items():
        path = _evidence_path(staged, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(record["base64"]))
    return _publish_import(store, staged, work_id)


def _publish_import(store: Store, staged: Path, work_id: str) -> Path:
    # Publication is journalled by the runtime integration layer.
    from .transactions import FileTransaction
    with file_lock(store.root / ".state"):
        target = store.active / work_id
        if target.exists():
            raise ConflictError(f"Handoff was already imported as {work_id}")
        index = store._validate_index()
        index["active"].append({"id": work_id, "path": f"active/{work_id}"})
        index["focused"] = work_id
        store._schema_validate("index", index)
        transaction = FileTransaction.begin(store.root, label=f"import:{work_id}")
        for file in sorted(staged.rglob("*")):
            if file.is_file():
                transaction.move(file, target / file.relative_to(staged))
        transaction.write(store.index_path, yaml.safe_dump(index, sort_keys=False, allow_unicode=True))
        transaction.apply()
        for directory in sorted((p for p in staged.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        staged.rmdir()
        return target
