from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock, SoftFileLock
from filelock import Timeout as FileLockTimeout


def operation_lock_uses_soft_fallback(lock_path: Path) -> bool:
    """Detect only a selected soft backend without probing the filesystem."""
    if FileLock is SoftFileLock:
        return True
    try:
        lock = FileLock(lock_path, timeout=0, mode=0o600)
    except (OSError, TypeError):
        # Constructor failures must not create an unlocked read path.
        return False
    return isinstance(lock, SoftFileLock)


def operation_lock_supported(lock_path: Path) -> bool:
    if operation_lock_uses_soft_fallback(lock_path):
        return False

    probe_parent = lock_path.parent
    while not probe_parent.exists():
        parent = probe_parent.parent
        if parent == probe_parent:
            return False
        probe_parent = parent

    try:
        # First-run settings can precede creation of the configured state directory.
        # Its nearest existing ancestor is the filesystem that will contain it.
        with TemporaryDirectory(prefix=".eom-lock-probe-", dir=probe_parent) as directory:
            probe = FileLock(Path(directory) / "probe.lock", timeout=0, mode=0o600)
            try:
                probe.acquire()
                return not isinstance(probe, SoftFileLock)
            finally:
                if probe.is_locked:
                    probe.release()
    except (FileLockTimeout, OSError):
        return False


@contextmanager
def operation_lock(lock_path: Path, busy_message: str) -> Iterator[None]:
    """Hold one native, nonblocking process lock on supported desktop platforms."""
    if FileLock is SoftFileLock:
        raise RuntimeError("Production operation locking is not available on this platform")

    lock = FileLock(lock_path, timeout=0, mode=0o600)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise RuntimeError(busy_message) from exc
    if isinstance(lock, SoftFileLock):
        lock.release()
        raise RuntimeError("Production operation locking is not available on this platform")
    try:
        yield
    finally:
        lock.release()
