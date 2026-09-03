"""Windows per-user storage primitives for Local Connect."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import uuid
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - imported only on non-Windows hosts
    msvcrt = None  # type: ignore[assignment]


REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_LOCK_OFFSET = 0
WINDOWS_LOCK_LENGTH = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ACL_SIZE_INFORMATION_CLASS = 2
_INHERIT_ONLY_ACE = 0x08
_ACCESS_ALLOWED_ACE_TYPES = frozenset({0x00, 0x05, 0x09, 0x0B})
_ACCESS_ALLOWED_COMPOUND_ACE_TYPE = 0x04
_ACE_OBJECT_TYPE_PRESENT = 0x00000001
_ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x00000002
_SENSITIVE_FILE_ACCESS = (
    0x00000001  # FILE_READ_DATA / FILE_LIST_DIRECTORY
    | 0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
    | 0x80000000  # GENERIC_READ
)
_TRUSTED_FIXED_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
_OWNER_PLACEHOLDER_SIDS = frozenset({"S-1-3-0", "S-1-3-4"})


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", ctypes.c_uint16),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("ace_count", ctypes.c_uint32),
        ("acl_bytes_in_use", ctypes.c_uint32),
        ("acl_bytes_free", ctypes.c_uint32),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class WindowsLockBusy(OSError):
    """The selected Windows byte range is already locked."""


@lru_cache(maxsize=1)
def _windows_libraries() -> tuple[object, object]:
    if os.name != "nt":
        raise OSError(errno.ENOSYS, "Windows ACL inspection is unavailable")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = ctypes.c_int
    advapi32.SetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    advapi32.GetAclInformation.restype = ctypes.c_int
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi32.IsValidSid.restype = ctypes.c_int
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = ctypes.c_uint32
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.GetTokenInformation.restype = ctypes.c_int
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return advapi32, kernel32


def _open_windows_shared_reader(path: Path) -> int:
    """Open a reader that cannot block atomic replacement or cleanup."""
    if os.name != "nt" or msvcrt is None:
        raise OSError(errno.ENOSYS, "Windows shared file reading is unavailable")
    _, kernel32 = _windows_libraries()
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _windows_error("Windows file could not be opened for shared reading")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _windows_error(message: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code or errno.EACCES, message)


def _sid_string(sid: ctypes.c_void_p) -> str:
    advapi32, kernel32 = _windows_libraries()
    value = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise _windows_error("Windows SID conversion failed")
    try:
        if not value.value:
            raise OSError(errno.EINVAL, "Windows SID is empty")
        return value.value.upper()
    finally:
        kernel32.LocalFree(ctypes.cast(value, ctypes.c_void_p))


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    advapi32, kernel32 = _windows_libraries()
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise _windows_error("Windows process token is unavailable")
    try:
        required = ctypes.c_uint32()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            raise _windows_error("Windows token user is unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise _windows_error("Windows token user is unavailable")
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_string(user.user.sid)
    finally:
        kernel32.CloseHandle(token)


def _protect_windows_directory(path: Path) -> None:
    """Give a newly created directory one protected, owner-private DACL."""
    advapi32, kernel32 = _windows_libraries()
    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint32()
    dacl = ctypes.c_void_p()
    dacl_present = ctypes.c_int()
    dacl_defaulted = ctypes.c_int()
    user_sid = _current_user_sid()
    sddl = (
        "D:P"
        "(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)"
        f"(A;OICI;FA;;;{user_sid})"
    )
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_error("Windows private security descriptor could not be built")
    try:
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise _windows_error("Windows private DACL could not be read")
        if not dacl_present.value or not dacl.value:
            raise OSError(errno.EACCES, "Windows private DACL is unavailable")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(result, "Windows private DACL could not be applied")
    finally:
        kernel32.LocalFree(descriptor)


def _private_windows_acl(path: Path) -> bool:
    advapi32, kernel32 = _windows_libraries()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, "Windows security descriptor is unavailable")
    try:
        if not owner.value or not dacl.value:
            return False
        current_user = _current_user_sid()
        trusted = _TRUSTED_FIXED_SIDS | {current_user}
        if _sid_string(owner) not in trusted:
            return False

        information = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise _windows_error("Windows ACL information is unavailable")
        for index in range(information.ace_count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise _windows_error("Windows ACL entry is unavailable")
            address = ace.value
            if address is None:
                return False
            header = _AceHeader.from_address(address)
            if header.ace_size < 8:
                return False
            mask = ctypes.c_uint32.from_address(address + 4).value
            if not mask & _SENSITIVE_FILE_ACCESS:
                continue
            if header.ace_type == _ACCESS_ALLOWED_COMPOUND_ACE_TYPE:
                return False
            if header.ace_type not in _ACCESS_ALLOWED_ACE_TYPES:
                continue
            sid_offset = 8
            if header.ace_type in {0x05, 0x0B}:
                if header.ace_size < 12:
                    return False
                object_flags = ctypes.c_uint32.from_address(address + 8).value
                sid_offset = 12
                if object_flags & _ACE_OBJECT_TYPE_PRESENT:
                    sid_offset += 16
                if object_flags & _ACE_INHERITED_OBJECT_TYPE_PRESENT:
                    sid_offset += 16
            if sid_offset >= header.ace_size:
                return False
            sid = ctypes.c_void_p(address + sid_offset)
            if not advapi32.IsValidSid(sid):
                return False
            if sid_offset + advapi32.GetLengthSid(sid) > header.ace_size:
                return False
            trustee = _sid_string(sid)
            if trustee in trusted:
                continue
            if (
                trustee in _OWNER_PLACEHOLDER_SIDS
                and header.ace_flags & _INHERIT_ONLY_ACE
            ):
                continue
            return False
        return True
    finally:
        kernel32.LocalFree(descriptor)


def local_app_data_root(value: str | None = None) -> Path:
    """Return an existing, absolute, non-reparse Local AppData directory."""
    configured = os.environ.get("LOCALAPPDATA") if value is None else value
    if not configured:
        raise OSError(errno.ENOENT, "LOCALAPPDATA is required")
    root = Path(configured)
    if not root.is_absolute():
        raise OSError(errno.EINVAL, "LOCALAPPDATA must be absolute")
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _is_reparse(metadata)
        or not _private_windows_acl(root)
    ):
        raise OSError(errno.EACCES, "LOCALAPPDATA is not a safe directory")
    return root


def ensure_private_directory(path: Path, *, root: Path) -> Path:
    """Create and verify Connect-owned directories below a trusted root."""
    root = Path(root)
    destination = Path(path)
    root_metadata = root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or _is_reparse(root_metadata)
        or not _private_windows_acl(root)
    ):
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
        created = False
        try:
            current.mkdir()
            created = True
        except FileExistsError:
            pass
        if created:
            _protect_windows_directory(current)
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _is_reparse(metadata)
            or not _private_windows_acl(current)
        ):
            raise OSError(errno.EACCES, "private directory is unsafe")
    return destination


def validate_private_directory(path: Path, *, root: Path) -> Path:
    """Verify an existing Connect directory chain below a trusted root."""
    root = Path(root)
    destination = Path(path)
    root_metadata = root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or _is_reparse(root_metadata)
        or not _private_windows_acl(root)
    ):
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
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _is_reparse(metadata)
            or not _private_windows_acl(current)
        ):
            raise OSError(errno.EACCES, "private directory is unsafe")
    return destination


def read_bounded_regular_file(
    path: Path,
    maximum: int,
    *,
    allow_empty: bool = False,
    require_private_acl: bool = True,
) -> bytes:
    """Read one stable, bounded Windows file without accepting reparse points."""
    candidate = Path(path)
    before = candidate.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse(before)
        or before.st_size > maximum
        or (before.st_size == 0 and not allow_empty)
        or (require_private_acl and not _private_windows_acl(candidate))
    ):
        raise OSError(errno.EINVAL, "file is not a bounded regular file")

    descriptor = _open_windows_shared_reader(candidate)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (require_private_acl and not _private_windows_acl(candidate))
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
        not stat.S_ISREG(existing.st_mode)
        or _is_reparse(existing)
        or not _private_windows_acl(destination)
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
        if not _private_windows_acl(temporary):
            raise OSError(errno.EACCES, "replacement temporary ACL is unsafe")
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
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _is_reparse(metadata)
        or not _private_windows_acl(candidate)
    ):
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
                or not _private_windows_acl(self.path)
            ):
                raise OSError(errno.EACCES, "lock file is unsafe")
            if metadata.st_size < WINDOWS_LOCK_LENGTH:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, WINDOWS_LOCK_LENGTH)
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
            os.lseek(descriptor, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, WINDOWS_LOCK_LENGTH)
        finally:
            os.close(descriptor)


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)
