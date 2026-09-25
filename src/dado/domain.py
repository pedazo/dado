"""Pure DAG and state-transition rules."""
from __future__ import annotations

from collections import defaultdict, deque
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from .errors import ValidationError

TASK_STATES = {"pending", "ready", "running", "review", "done", "blocked", "failed", "stale", "cancelled"}
TRANSITIONS = {
    "pending": {"ready", "blocked", "cancelled"},
    "ready": {"running", "blocked", "cancelled", "stale"},
    "running": {"review", "blocked", "failed", "ready", "stale"},
    "review": {"done", "failed", "blocked", "ready", "stale"},
    "done": {"stale"},
    "blocked": {"ready", "cancelled", "stale"},
    "failed": {"ready", "cancelled", "stale"},
    "stale": {"pending", "ready", "cancelled"},
    "cancelled": set(),
}


def validate_dag(tasks: list[dict[str, Any]]) -> list[str]:
    by_id = {task.get("id"): task for task in tasks}
    if None in by_id or len(by_id) != len(tasks):
        raise ValidationError("Task IDs must be present and unique")
    indegree = {key: 0 for key in by_id}
    children: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        status = task.get("status")
        if status not in TASK_STATES:
            raise ValidationError(f"{task.get('id')} has invalid status {status!r}")
        deps = task.get("depends_on", [])
        if len(deps) != len(set(deps)):
            raise ValidationError(f"{task['id']} contains duplicate dependencies")
        for dep in deps:
            if dep not in by_id:
                raise ValidationError(f"{task['id']} depends on missing task {dep}")
            if dep == task["id"]:
                raise ValidationError(f"{task['id']} cannot depend on itself")
            indegree[task["id"]] += 1
            children[dep].append(task["id"])
        # A running task may not have unfinished dependencies.
        if status in {"running", "review", "done"} and any(by_id[d]["status"] != "done" for d in deps):
            raise ValidationError(f"{task['id']} is {status} while dependencies are not done")
    queue = deque(k for k, n in indegree.items() if n == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(tasks):
        raise ValidationError("Task plan contains a dependency cycle")
    return order


def ready_tasks(tasks: list[dict[str, Any]], max_parallel: int = 3) -> list[dict[str, Any]]:
    validate_dag(tasks)
    running = sum(t["status"] in {"running", "review"} for t in tasks)
    slots = max(0, max_parallel - running)
    if slots == 0:
        return []
    active_scopes = [t.get("scope", {}).get("allowed", []) for t in tasks if t["status"] in {"running", "review"}]
    selected: list[dict[str, Any]] = []
    for task in tasks:
        if task["status"] not in {"pending", "ready"}:
            continue
        if not all(next(x for x in tasks if x["id"] == dep)["status"] == "done" for dep in task.get("depends_on", [])):
            continue
        scope = task.get("scope", {}).get("allowed", [])
        if any(scopes_overlap(scope, other) for other in [*active_scopes, *(x.get("scope", {}).get("allowed", []) for x in selected)]):
            continue
        selected.append(task)
        if len(selected) >= slots:
            break
    return selected


def scopes_overlap(left: list[str], right: list[str]) -> bool:
    """Conservative overlap detection for exact paths, directories and glob scopes."""
    for a in left:
        for b in right:
            aa = a.replace("\\", "/").removeprefix("./").rstrip("/")
            bb = b.replace("\\", "/").removeprefix("./").rstrip("/")
            if aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/"):
                return True
            # Glob intersection is hard to prove disjoint (e.g. mid-segment `src*`),
            # so serialize any task using globs against any other declared scope.
            if any(char in aa + bb for char in "*?["):
                return True
    return False


def scope_matches(path: str, pattern: str) -> bool:
    """Match project-relative globs without allowing '*' to cross directory separators."""
    candidate = PurePosixPath(path.replace("\\", "/").removeprefix("./")).parts
    scope = PurePosixPath(pattern.replace("\\", "/").removeprefix("./")).parts

    def matches(i: int, j: int) -> bool:
        if j == len(scope):
            return i == len(candidate)
        if scope[j] == "**":
            return matches(i, j + 1) or (i < len(candidate) and matches(i + 1, j))
        return i < len(candidate) and fnmatchcase(candidate[i], scope[j]) and matches(i + 1, j + 1)

    if matches(0, 0):
        return True
    # A literal directory scope also grants its descendants.
    return (not any(char in pattern for char in "*?[") and len(candidate) > len(scope)
            and candidate[:len(scope)] == scope)


def transition(task: dict[str, Any], target: str) -> dict[str, Any]:
    source = task.get("status")
    if target not in TASK_STATES or target not in TRANSITIONS.get(source, set()):
        raise ValidationError(f"Invalid task transition: {source} -> {target}")
    task["status"] = target
    return task


def invalidate_closure(tasks: list[dict[str, Any]], task_id: str) -> list[str]:
    by_id = {t["id"]: t for t in tasks}
    if task_id not in by_id:
        raise ValidationError(f"Unknown task {task_id}")
    affected = {task_id}
    while True:
        more = {t["id"] for t in tasks if any(dep in affected for dep in t.get("depends_on", []))}
        new = more - affected
        if not new:
            break
        affected |= new
    for item in affected:
        if by_id[item]["status"] not in {"cancelled", "stale"}:
            by_id[item]["status"] = "stale"
    return sorted(affected)
