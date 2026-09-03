"""Windows per-user storage primitives for Local Connect."""

from __future__ import annotations

import errno
import os
import stat
import uuid
from contextlib import suppress
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - imported only on non-Windows hosts
    msvcrt = None  # type: ignore[assignment]


REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WindowsLockBusy(OSError):
    """The selected Windows byte range is already locked."""


def local_app_data_root(value: str | None = None) -> Path:
    """Return an existing, absolute, non-reparse Local AppData directory."""
    configured = os.environ.get("LOCALAPPDATA") if value is None else value
    if not configured:
        raise OSError(errno.ENOENT, "LOCALAPPDATA is required")
    root = Path(configured)
    if not root.is_absolute():
        raise OSError(errno.EINVAL, "LOCALAPPDATA must be absolute")
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
        raise OSError(errno.EACCES, "LOCALAPPDATA is not a safe directory")
    return root


def ensure_private_directory(path: Path, *, root: Path) -> Path:
    """Create and verify Connect-owned directories below a trusted root."""
    root = Path(root)
    destination = Path(path)
    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or _is_reparse(root_metadata):
        raise OSError(errno.EACCES, "private storage root is unsafe")
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise OSError(errno.EACCES, "private directory escapes its root") from exc

    current = root
    for component in relative.parts:
        if component in ("", os.curdir, os.pardir):
            raise OSError(errno.EINVAL, "private directory component is unsafe")
        current = current / component
        with suppress(FileExistsError):
            current.mkdir()
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
            raise OSError(errno.EACCES, "private directory is unsafe")
    return destination


def validate_private_directory(path: Path, *, root: Path) -> Path:
    """Verify an existing Connect directory chain below a trusted root."""
    root = Path(root)
    destination = Path(path)
    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or _is_reparse(root_metadata):
        raise OSError(errno.EACCES, "private storage root is unsafe")
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise OSError(errno.EACCES, "private directory escapes its root") from exc

    current = root
    for component in relative.parts:
        if component in ("", os.curdir, os.pardir):
            raise OSError(errno.EINVAL, "private directory component is unsafe")
        current = current / component
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
            raise OSError(errno.EACCES, "private directory is unsafe")
    return destination


def read_bounded_regular_file(
    path: Path, maximum: int, *, allow_empty: bool = False
) -> bytes:
    """Read one stable, bounded Windows file without accepting reparse points."""
    candidate = Path(path)
    before = candidate.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse(before)
        or before.st_size > maximum
        or (before.st_size == 0 and not allow_empty)
    ):
        raise OSError(errno.EINVAL, "file is not a bounded regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError(errno.EINVAL, "opened file changed identity")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(8192, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) != before.st_size
            or len(content) > maximum
            or (not content and not allow_empty)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise OSError(errno.EINVAL, "file changed while being read")
        return bytes(content)
    finally:
        os.close(descriptor)


def atomic_replace_bytes(
    destination: Path,
    content: bytes,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> None:
    """Flush bytes to a same-directory temporary file and atomically replace."""
    if len(content) > maximum or (not content and not allow_empty):
        raise OSError(errno.EFBIG, "replacement content is empty or oversized")
    destination = Path(destination)
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or _is_reparse(existing)
    ):
        raise OSError(errno.EACCES, "replacement destination is unsafe")

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    descriptor: int | None = None
    promoted = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "replacement write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        promoted = True
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if not promoted:
            with suppress(OSError):
                temporary.unlink()


def unlink_regular_file(path: Path) -> None:
    """Remove only a regular, non-reparse file."""
    candidate = Path(path)
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
        raise OSError(errno.EACCES, "refusing to remove an unsafe path")
    candidate.unlink()


class WindowsFileLock:
    """Non-blocking one-byte advisory lock shared by Connect applications."""

    def __init__(self, path: Path) -> None:
        if os.name != "nt" or msvcrt is None:
            raise OSError(errno.ENOSYS, "Windows file locking is unavailable")
        self.path = Path(path)
        self._descriptor: int | None = None
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        descriptor = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            visible = self.path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _is_reparse(visible)
                or (metadata.st_dev, metadata.st_ino)
                != (visible.st_dev, visible.st_ino)
            ):
                raise OSError(errno.EACCES, "lock file is unsafe")
            if metadata.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise WindowsLockBusy(exc.errno, "file lock is busy") from exc
                raise
            self._descriptor = descriptor
        except Exception:
            os.close(descriptor)
            raise

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)
