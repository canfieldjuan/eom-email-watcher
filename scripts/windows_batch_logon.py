"""Single-right lease for a newly created disposable qualification SID."""

import ctypes
import os


def set_batch_logon_right(sid, grant):
    assert os.name == "nt"
    pointer, word = ctypes.c_void_p, ctypes.c_uint32

    class Attributes(ctypes.Structure):
        _fields_ = [
            ("length", word),
            ("root", pointer),
            ("name", pointer),
            ("attributes", word),
            ("descriptor", pointer),
            ("quality", pointer),
        ]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint16), ("maximum", ctypes.c_uint16), ("buffer", pointer)]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(pointer)]
    api.ConvertStringSidToSidW.restype = ctypes.c_int
    api.LsaOpenPolicy.argtypes = [
        pointer,
        ctypes.POINTER(Attributes),
        word,
        ctypes.POINTER(pointer),
    ]
    api.LsaOpenPolicy.restype = word
    api.LsaAddAccountRights.argtypes = [pointer, pointer, ctypes.POINTER(UnicodeString), word]
    api.LsaAddAccountRights.restype = word
    api.LsaRemoveAccountRights.argtypes = [
        pointer,
        pointer,
        ctypes.c_ubyte,
        ctypes.POINTER(UnicodeString),
        word,
    ]
    api.LsaRemoveAccountRights.restype = word
    api.LsaNtStatusToWinError.argtypes = [word]
    api.LsaNtStatusToWinError.restype = word
    api.LsaClose.argtypes = [pointer]
    kernel.LocalFree.argtypes = [pointer]
    native_sid, policy = pointer(), pointer()
    if not api.ConvertStringSidToSidW(sid, ctypes.byref(native_sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = Attributes()
        attributes.length = ctypes.sizeof(attributes)
        status = api.LsaOpenPolicy(None, ctypes.byref(attributes), 0x810, ctypes.byref(policy))
        if status:
            raise ctypes.WinError(api.LsaNtStatusToWinError(status))
        right = ctypes.create_unicode_buffer("SeBatchLogonRight")
        length = len(right.value) * ctypes.sizeof(ctypes.c_wchar)
        value = UnicodeString(
            length, length + ctypes.sizeof(ctypes.c_wchar), ctypes.cast(right, pointer)
        )
        status = (
            api.LsaAddAccountRights(policy, native_sid, ctypes.byref(value), 1)
            if grant
            else api.LsaRemoveAccountRights(policy, native_sid, 0, ctypes.byref(value), 1)
        )
        if status and not (not grant and api.LsaNtStatusToWinError(status) == 2):
            raise ctypes.WinError(api.LsaNtStatusToWinError(status))
    finally:
        if policy:
            api.LsaClose(policy)
        kernel.LocalFree(native_sid)
