"""Native public process permission boundary, only for installer qualification."""

import ctypes
import os
from contextlib import contextmanager


@contextmanager
def deny_termination(process):
    """Keep the CreateProcess handle to restore this fixture's original DACL."""
    assert os.name == "nt"
    from connect_automate.connect_windows import _current_user_sid

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer, word = ctypes.c_void_p, ctypes.c_uint32
    advapi.GetSecurityInfo.argtypes = [
        pointer,
        word,
        word,
        pointer,
        pointer,
        ctypes.POINTER(pointer),
        pointer,
        ctypes.POINTER(pointer),
    ]
    advapi.GetSecurityInfo.restype = word
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        word,
        ctypes.POINTER(pointer),
        pointer,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    advapi.SetKernelObjectSecurity.argtypes = [pointer, word, pointer]
    advapi.SetKernelObjectSecurity.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    handle = pointer(int(process._handle))
    original, acl, denied = pointer(), pointer(), pointer()
    error = advapi.GetSecurityInfo(
        handle, 6, 4, None, None, ctypes.byref(acl), None, ctypes.byref(original)
    )
    if error:
        raise ctypes.WinError(error)
    sid = _current_user_sid()
    try:
        descriptor = f"D:(D;;0x00000001;;;{sid})(A;;GA;;;{sid})(A;;GA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor, 1, ctypes.byref(denied), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.SetKernelObjectSecurity(handle, 4, denied):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not advapi.SetKernelObjectSecurity(handle, 4, original):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(original)
        if denied:
            kernel.LocalFree(denied)
