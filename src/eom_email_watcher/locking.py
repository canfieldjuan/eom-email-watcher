from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def operation_lock(lock_path: Path, busy_message: str) -> Iterator[None]:
    """Hold the existing Unix advisory lock behind a platform adapter boundary."""
    if os.name != "posix":
        raise RuntimeError("Production operation locking is not available on this platform")

    import fcntl

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(busy_message) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
