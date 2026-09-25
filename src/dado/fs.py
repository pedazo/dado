"""Safe local filesystem primitives."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from .errors import ConflictError


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_yaml(path: Path, value: Any) -> None:
    atomic_write(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=100))


def read_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (yaml.YAMLError, OSError) as exc:
        raise ConflictError(f"Cannot read valid YAML from {path}: {exc}") from exc


def append_event(root: Path, event: str, **data: Any) -> None:
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
    # append writes are small; O_APPEND prevents interleaving across local writers.
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Cross-platform exclusive lock using atomic lock-directory creation."""
    lockdir = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lockdir.mkdir(parents=False)
            break
        except FileExistsError:
            # A stale lock is only reclaimed after a conservative age threshold.
            try:
                if time.time() - lockdir.stat().st_mtime > 300:
                    lockdir.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise ConflictError(f"Timed out waiting for state lock: {lockdir}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lockdir.rmdir()
        except OSError:
            pass
