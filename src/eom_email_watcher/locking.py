from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, SoftFileLock
from filelock import Timeout as FileLockTimeout


def operation_lock_supported() -> bool:
    return FileLock is not SoftFileLock


@contextmanager
def operation_lock(lock_path: Path, busy_message: str) -> Iterator[None]:
    """Hold one native, nonblocking process lock on supported desktop platforms."""
    if not operation_lock_supported():
        raise RuntimeError("Production operation locking is not available on this platform")

    lock = FileLock(lock_path, timeout=0, mode=0o600)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise RuntimeError(busy_message) from exc
    try:
        yield
    finally:
        lock.release()
