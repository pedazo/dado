"""Pure task-contract hashing helpers (no filesystem access).

Kept separate from the Store so other components (verifiers, CLI, tests)
can reproduce a task's contract hash without importing persistence code or
touching artifacts. The hash covers everything about a task and its
satisfied requirements EXCEPT execution state, so any later edit to the
plan documents is detectable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Keys that describe execution state rather than the planned contract.
MUTABLE_TASK_KEYS = {"status", "attempts", "verified_at", "result", "verdict", "started_contract_hash"}


def stable_task(task: dict[str, Any]) -> dict[str, Any]:
    """The contract portion of a task packet (execution state stripped)."""
    return {key: value for key, value in task.items() if key not in MUTABLE_TASK_KEYS}


def stable_requirement(requirement: dict[str, Any]) -> dict[str, Any]:
    """A requirement's contract portion (current status is mutable state)."""
    return {key: value for key, value in requirement.items() if key != "status"}


def contract_payload(task: dict[str, Any], requirements: list[dict[str, Any]]) -> dict[str, Any]:
    """The canonical payload hashed for one running task.

    ``requirements`` are the requirement dicts this task satisfies (already
    filtered by the caller); only their contract fields enter the hash.
    """
    return {"task": stable_task(task),
            "requirements": [stable_requirement(requirement) for requirement in requirements]}


def hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def task_contract_hash(task: dict[str, Any], requirements: list[dict[str, Any]]) -> str:
    """Hash of the task's contract and the requirements it satisfies."""
    return hash_payload(contract_payload(task, requirements))
