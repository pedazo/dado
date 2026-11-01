"""Reproducible local runtime microbenchmark; never invokes a model or network.

Use PYTHONPATH to compare another checkout with the same fixture and command.
Each operation reports time and fsync calls; filesystem/hardware affect timings.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from dado import fs
from dado.domain import invalidate_closure, ready_tasks
from dado.store import Store


def run(size: int) -> dict:
    tasks = [{"id": f"T-{i:03d}", "title": f"Task {i}", "goal": "Benchmark fixture",
              "status": "pending", "satisfies": ["REQ-001"], "depends_on": [],
              "scope": {"allowed": [f"src/part{i}"], "forbidden": []},
              "context_refs": [], "acceptance": ["Fixture passes"],
              "verification": ["Fixture check"], "risk": "low", "attempts": 0}
             for i in range(1, size + 1)]
    measurements = {}

    def measure(name, operation):
        original = fs.os.fsync
        with patch.object(fs.os, "fsync", wraps=original) as sync:
            start = time.perf_counter()
            operation()
            measurements[name] = {"seconds": time.perf_counter() - start, "fsync_calls": sync.call_count}

    with tempfile.TemporaryDirectory(prefix="dado-benchmark-") as directory:
        store = Store(Path(directory))
        work = store.create_work("Benchmark fixture", "Synthetic runtime benchmark")
        store.add_requirement(work.name, {"id": "REQ-001", "description": "Fixture", "status": "pending", "source": "user", "acceptance": ["Fixture passes"]})
        store.save_tasks(work, tasks)
        measure("load", lambda: store.load_tasks(work))
        measure("ready", lambda: store.ready(work.name))
        measure("unchanged_save", lambda: store.save_tasks(work, tasks))
        tasks[0]["status"] = "ready"
        measure("one_task_save", lambda: store.save_tasks(work, tasks))
        store.set_work_status(work.name, "awaiting_approval")
        measure("approve", lambda: store.approve_work(work.name, "Benchmark fixture approval"))
        measure("start", lambda: store.set_task_state(work.name, tasks[0]["id"], "running"))
        measure("result", lambda: store.record_result(work.name, {
            "task": tasks[0]["id"], "status": "completed", "changed": [],
            "verification": {"passed": True, "checks": ["Fixture"]},
            "assumptions": [], "issues": [], "evidence": [], "summary": "Synthetic fixture"}))
        measure("invalidate", lambda: store.invalidate(work.name, tasks[0]["id"], "Benchmark"))
        store.set_work_status(work.name, "cancelled")
        measure("archive_cancelled", lambda: store.archive(work.name))
    chain = [{"id": str(i), "status": "done", "depends_on": [str(i - 1)] if i else []} for i in range(size)]
    measure("invalidate_chain", lambda: invalidate_closure(chain, "0"))
    measure("ready_pure", lambda: ready_tasks(tasks))
    return {"tasks": size, "operations": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 100, 500])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("sizes must be positive")
    result = {"kind": "runtime-microbenchmark", "model_calls": 0, "runs": [run(size) for size in args.sizes]}
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
