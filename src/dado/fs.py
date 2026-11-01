"""Safe local filesystem primitives.

Concurrency model
-----------------
``file_lock`` provides a cross-platform exclusive lock that serializes both
threads *and* processes:

* OS-level exclusion is done with true advisory file locks from the standard
  library only — ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.
  Ownership is held by an open file descriptor, so the kernel releases the
  lock automatically when the owning process exits (even if that process is
  killed). There is deliberately **no mtime-age-based reclamation**: a lock
  that is still held by a live process is never stolen just because it looks
  old; once the holder is dead the lock is free immediately.
* In-process exclusion is provided by a per-lock-path ``threading.Lock``
  registry, because POSIX ``flock`` is per-process and would otherwise not
  serialize two threads of the same process.

Lock files (``<path>.lock`` beside the guarded path) are persistent and are
never unlinked while or after being held; ownership lives purely in the
kernel, which avoids all classic unlink races. A leftover lock file with no
live holder can be acquired immediately.

Nested locking note: the same thread acquiring the *same* lock path again
while already holding it deadlocks (RLock semantics are intentionally not
used because the underlying OS lock does not nest on the second descriptor).
Acquiring *different* lock paths in a fixed order is safe.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from .errors import ConflictError

try:  # POSIX advisory locks (Linux, macOS, BSDs)
    import fcntl  # noqa: F401  (import is required at module scope for typing clarity)

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows build
    _HAVE_FCNTL = False

try:  # Windows region locks
    import msvcrt  # noqa: F401

    _HAVE_MSVCRT = True
except ImportError:
    _HAVE_MSVCRT = False

if not _HAVE_FCNTL and not _HAVE_MSVCRT:  # pragma: no cover - exotic platform
    raise ImportError("dado.fs requires fcntl (POSIX) or msvcrt (Windows)")

_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def fsync_directory(dirpath: Path) -> None:
    """Flush a directory entry to stable storage where the OS supports it.

    Best-effort and harmless on platforms that reject directory descriptors
    (Windows): any ``OSError`` is swallowed.
    """
    try:
        fd = os.open(dirpath, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def atomic_write(path: Path, data: str) -> None:
    """Replace ``path`` with ``data`` atomically and durably.

    The payload is written to a sibling temporary file, flushed and fsynced,
    then moved into place with ``os.replace`` (atomic within the enclosing
    directory). The parent directory is fsynced afterwards so the rename is
    durable where the platform supports directory fsyncs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
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
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise ConflictError(f"Cannot read valid YAML from {path}: {exc}") from exc


def append_event(root: Path, event: str, **data: Any) -> None:
    """Append one JSON record to ``root/events.jsonl``.

    The record is fully serialized *before* the file is touched, so a
    serialization failure (e.g. a non-JSON-serializable payload) cannot leave
    a partial line behind. The write then happens under an exclusive file
    lock so records from concurrent threads and processes never interleave,
    the full payload is written even if ``os.write`` performs a partial
    write, and the file is fsynced before the call returns.
    """
    path = Path(root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with file_lock(path, timeout=10.0):
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:  # pragma: no cover - defensive
                    raise OSError(f"Append to {path} made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def _thread_guard(lockfile: Path) -> threading.Lock:
    """Return the in-process mutex serializing threads for one lock path."""
    key = str(lockfile)
    with _THREAD_LOCK_GUARD:
        guard = _THREAD_LOCKS.get(key)
        if guard is None:
            guard = threading.Lock()
            _THREAD_LOCKS[key] = guard
        return guard


def _os_lock(fd: int, lockfile: Path) -> None:
    """Take the exclusive OS lock on one descriptor, non-blocking (caller retries)."""
    if _HAVE_MSVCRT:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    try:
        if _HAVE_MSVCRT:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # best effort; closing the descriptor releases it anyway


def _open_lockfile(lockfile: Path) -> int:
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"x")  # msvcrt needs a byte to lock
            os.fsync(fd)
    except OSError:
        pass
    return fd


@contextmanager
def file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold an exclusive lock serializing threads *and* processes.

    ``path`` is the guarded resource (it does not need to exist); the actual
    OS-lock target is the persistent sibling ``<path>.lock`` file whose parent
    directory is created as needed.

    Blocks at most ``timeout`` seconds, then raises ``ConflictError`` —
    including when the conflicting lock looks old but is still genuinely
    held by a live process. Old lock files without a live holder can be
    acquired immediately; ownership is released automatically by the OS when
    the holder exits, so no lock is ever held without a live owner and no
    age-based takeover exists by design.
    """
    lockfile = path.with_name(path.name + ".lock")
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    guard = _thread_guard(lockfile)
    deadline = time.monotonic() + timeout
    if not guard.acquire(timeout=max(deadline - time.monotonic(), 0.0)):
        raise ConflictError(f"Timed out waiting for in-process lock holder: {lockfile}")
    fd: int | None = None
    try:
        fd = _open_lockfile(lockfile)
        while True:
            try:
                _os_lock(fd, lockfile)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ConflictError(f"Timed out waiting for state lock: {lockfile}") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                _os_unlock(fd)
                os.close(fd)
                fd = None
    finally:
        if fd is not None:  # exception while acquiring — close what we opened
            os.close(fd)
        guard.release()
