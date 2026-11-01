"""Opt-in durable admission budgets; actual provider billing remains external.

An event-sourced local ledger preserves reservations across session restarts.
Finite cost caps refuse reservations with unknown cost. Settlement may report
actual overspend, but cannot undo an external provider call already made.
"""
from __future__ import annotations

from pathlib import Path

from .errors import ConflictError, ValidationError
from .fs import file_lock, read_yaml, write_yaml
from .policy import BudgetLedger


def _load(path: Path) -> tuple[dict, BudgetLedger]:
    value = read_yaml(path)
    if not isinstance(value, dict) or set(value) != {"version", "caps", "events"} or value["version"] != 1:
        raise ValidationError(f"Invalid budget ledger: {path}")
    if not isinstance(value["caps"], dict) or set(value["caps"]) != {"cost", "time_minutes", "retries"} or not isinstance(value["events"], list):
        raise ValidationError(f"Invalid budget caps/events: {path}")
    ledger = BudgetLedger(**value["caps"])
    for event in value["events"]:
        if not isinstance(event, dict) or set(event) != {"action", "arguments"} or event["action"] not in {"reserve", "charge", "release"} or not isinstance(event["arguments"], dict):
            raise ValidationError("Invalid budget event")
        try:
            getattr(ledger, event["action"])(**event["arguments"])
        except TypeError as exc:
            raise ValidationError("Invalid budget event arguments") from exc
    return value, ledger


def initialize(path: Path, *, cost: float | None, time_minutes: float, retries: int) -> dict:
    caps = {"cost": cost, "time_minutes": time_minutes, "retries": retries}
    ledger = BudgetLedger(**caps)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        if path.exists():
            raise ConflictError("Budget already exists; caps cannot be silently replaced")
        write_yaml(path, {"version": 1, "caps": caps, "events": []})
    return ledger.status()


def update(path: Path, action: str, **arguments) -> dict:
    if action not in {"reserve", "charge", "release"}:
        raise ValidationError("Unknown budget operation")
    with file_lock(path):
        value, ledger = _load(path)
        if action == "reserve" and value["caps"]["cost"] is not None and arguments.get("cost") is None:
            raise ValidationError("A finite cost cap requires a known reservation estimate; unknown is not free")
        try:
            result = getattr(ledger, action)(**arguments)
        except TypeError as exc:
            raise ValidationError("Invalid budget operation arguments") from exc
        value["events"].append({"action": action, "arguments": arguments})
        write_yaml(path, value)
        return {"result": result, "budget": ledger.status()}


def status(path: Path) -> dict:
    with file_lock(path):
        return _load(path)[1].status()


def require_reservation(path: Path, task_id: str) -> None:
    """Called at task admission only when a work has opted into a ledger."""
    if not path.exists():
        return
    data = status(path)
    if not any(item.get("phase") == task_id for item in data["reservations"]):
        raise ValidationError(f"Task {task_id} requires an active budget reservation before execution")
