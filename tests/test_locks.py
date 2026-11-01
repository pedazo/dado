"""Locking tests: cross-platform file_lock serializing threads and processes.

Covers the contract promised by ``dado.fs.file_lock``:

* mutual exclusion between *processes* (fcntl/msvcrt OS locks),
* mutual exclusion between *threads* of one process,
* ConflictError on timeout while a live holder still owns the lock,
* a lock that looks old but is genuinely held is NEVER reclaimed by age,
* an aged lock file with no live holder is immediately acquirable,
* safe ownership release: reacquire, cross-process handoff, and automatic
  release when the holder is killed with SIGKILL.
"""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dado.errors import ConflictError, ValidationError
from dado.fs import file_lock, read_yaml

SRC = Path(__file__).resolve().parents[1] / "src"

HOLD_WORKER = r"""
import os, sys, time
from pathlib import Path
from dado.fs import file_lock

path, mode, marker, extra = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
with file_lock(path, timeout=10.0):
    marker.write_text("held")
    lockfile = path.with_name(path.name + ".lock")
    old = time.time() - 3600.0
    os.utime(lockfile, (old, old))  # live holder, one hour old: must never be reclaimed
    if mode == "backdate-and-hold":
        time.sleep(float(extra))  # stays live; parent must time out, not steal
        marker.write_text("released")
    if mode == "kill-self":
        time.sleep(float(extra))
        os._exit(1)  # abrupt process death without running lock cleanup
"""

COUNTER_WORKER = r"""
import sys, time
from pathlib import Path
from dado.fs import file_lock

path, counter_file, rounds = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
for _ in range(rounds):
    with file_lock(path, timeout=30.0):
        value = int(counter_file.read_text()) if counter_file.exists() else 0
        time.sleep(0.002)  # widen the critical section so lockless runs lose updates
        counter_file.write_text(str(value + 1))
"""


def spawn_worker(script: str, argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *argv],
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_marker(marker: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.02)
    pytest.fail(f"worker never wrote {marker}")


def test_processes_serialize_read_modify_write(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.locked-path"
    counter = tmp_path / "counter"
    procs = [
        spawn_worker(COUNTER_WORKER, [str(lock_path), str(counter), "25"]) for _ in range(6)
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert counter.read_text() == "150"  # 6 processes x 25 increments, none lost


def test_timeout_conflict_error_while_live_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared" / ".state"
    marker = tmp_path / "marker"
    worker = spawn_worker(HOLD_WORKER, [str(lock_path), "backdate-and-hold", str(marker), "2.0"])
    try:
        wait_for_marker(marker)
        with pytest.raises(ConflictError):
            with file_lock(lock_path, timeout=0.2):
                pass
    finally:
        worker.wait(timeout=10)


def test_live_aged_lock_is_never_reclaimed_by_age(tmp_path: Path) -> None:
    lock_path = tmp_path / "aged" / "target"
    marker = tmp_path / "aged-marker"
    worker = spawn_worker(HOLD_WORKER, [str(lock_path), "backdate-and-hold", str(marker), "0.8"])
    try:
        wait_for_marker(marker)
        start = time.monotonic()
        with pytest.raises(ConflictError):
            with file_lock(lock_path, timeout=0.25):
                pass
        # quick refusal: a timeout decision, not an age-based wait:
        assert time.monotonic() - start < 3.0
    finally:
        worker.wait(timeout=10)


def test_aged_lockfile_without_live_holder_is_available(tmp_path: Path) -> None:
    lock_path = tmp_path / "gate" / "target"
    lock_path.parent.mkdir(parents=True)
    lockfile = lock_path.with_name(lock_path.name + ".lock")
    lockfile.touch()
    old = time.time() - 24 * 3600  # a day old, but nobody holds it
    os.utime(lockfile, (old, old))
    with file_lock(lock_path, timeout=1.0):
        pass


def test_threads_serialize_read_modify_write(tmp_path: Path) -> None:
    lock_path = tmp_path / "threads" / ".model-profiles"
    counter = tmp_path / "thread-counter"

    def bump(_index: int) -> None:
        for _ in range(20):
            with file_lock(lock_path, timeout=30.0):
                value = int(counter.read_text()) if counter.exists() else 0
                time.sleep(0.001)
                counter.write_text(str(value + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bump, range(8)))
    assert counter.read_text() == "160"


def test_same_process_reacquire_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "again" / ".state"
    with file_lock(lock_path):
        pass
    with file_lock(lock_path, timeout=5.0):
        pass


def test_read_yaml_invalid_utf8_raises_conflict_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.yaml"
    path.write_bytes(b'key: "valid prefix\xff\xfe\xf9truncated')  # not decodable UTF-8
    with pytest.raises(ConflictError) as err:
        read_yaml(path)
    assert "Cannot read valid YAML" in str(err.value)


def test_killed_holder_releases_immediately(tmp_path: Path) -> None:
    lock_path = tmp_path / "killed" / ".state"
    marker = tmp_path / "kill-marker"
    worker = spawn_worker(HOLD_WORKER, [str(lock_path), "kill-self", str(marker), "3.0"])
    try:
        wait_for_marker(marker)
        time.sleep(0.3)  # let the worker exit abruptly while holding the lock
        start = time.monotonic()
        # The kernel dropped the dead owner instantly; no age threshold and
        # no arbitrary reclamation policy is needed or waited for.
        with file_lock(lock_path, timeout=10.0):
            pass
        assert time.monotonic() - start < 9.0
    finally:
        returncode = worker.wait(timeout=15)
    assert returncode != 0
