from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock, SoftFileLock
from filelock import Timeout as FileLockTimeout


def _connect_lock_path(database_path: Path, kind: str, identity: tuple[str, ...]) -> Path:
    if any(not value for value in identity):
        raise ValueError("Connect lock identity cannot be empty")
    encoded = "\0".join((kind, *identity)).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return database_path.parent / ".connect-locks" / f"{kind}-{digest}.lock"


def connect_lane_lock_path(
    database_path: Path,
    *,
    protocol_version: int,
    provider_app_id: str,
    provider_instance_id: str,
) -> Path:
    return _connect_lock_path(
        database_path,
        "lane",
        (str(protocol_version), provider_app_id, provider_instance_id),
    )


def connect_source_lock_path(database_path: Path, message_id: str) -> Path:
    return _connect_lock_path(database_path, "source", (message_id,))


def prepare_private_lock_path(lock_path: Path) -> None:
    state_directory = lock_path.parent.parent
    if not state_directory.is_dir():
        raise RuntimeError("Connect state directory must be initialized before locking")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        lock_path.parent.chmod(0o700)


@contextmanager
def connect_operation_lock(lock_path: Path, busy_message: str) -> Iterator[None]:
    prepare_private_lock_path(lock_path)
    with operation_lock(lock_path, busy_message):
        yield


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
