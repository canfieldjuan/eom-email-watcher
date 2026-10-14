"""Environment owners for native Windows qualification, never product policy."""

import ctypes
import os
import uuid


def powershell_environment(base=None, **values):
    """Let Windows PowerShell construct its own module path, including nested hosts."""
    source = dict(os.environ if base is None else base, **values)
    return {key: value for key, value in source.items() if key.casefold() != "psmodulepath"}


def assert_profile_environment(environment, known_folders):
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        actual = environment.get(key)
        expected = known_folders.get(key)
        assert (
            actual
            and expected
            and os.path.normcase(os.path.normpath(actual))
            == os.path.normcase(os.path.normpath(expected))
        ), ("standard-user profile environment mismatch", key, actual, expected)


def profile_environment_report():
    """Compare inherited values with current-token Windows known folders."""
    assert os.name == "nt"
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    folders = {}
    for key, identifier in {
        "USERPROFILE": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
        "APPDATA": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
        "LOCALAPPDATA": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
    }.items():
        guid = ctypes.create_string_buffer(uuid.UUID(identifier).bytes_le)
        result = ctypes.c_void_p()
        status = shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(result))
        if status < 0:
            raise OSError(status, "Current-token known-folder query failed")
        try:
            folders[key] = ctypes.wstring_at(result)
        finally:
            ole.CoTaskMemFree(result)
    environment = {key: os.environ.get(key) for key in folders}
    assert_profile_environment(environment, folders)
    return {"environment": environment, "current_token_known_folders": folders}
