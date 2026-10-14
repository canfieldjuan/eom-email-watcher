"""Single-right lease for a newly created disposable qualification SID."""

import ctypes
import os
from contextlib import contextmanager


@contextmanager
def _account_policy(sid):
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
        yield api, policy, native_sid, UnicodeString
    finally:
        if policy:
            api.LsaClose(policy)
        kernel.LocalFree(native_sid)


def set_batch_logon_right(sid, grant):
    with _account_policy(sid) as (api, policy, native_sid, UnicodeString):
        right = ctypes.create_unicode_buffer("SeBatchLogonRight")
        length = len(right.value) * ctypes.sizeof(ctypes.c_wchar)
        value = UnicodeString(
            length, length + ctypes.sizeof(ctypes.c_wchar), ctypes.cast(right, ctypes.c_void_p)
        )
        status = (
            api.LsaAddAccountRights(policy, native_sid, ctypes.byref(value), 1)
            if grant
            else api.LsaRemoveAccountRights(policy, native_sid, 0, ctypes.byref(value), 1)
        )
        if status and not (not grant and api.LsaNtStatusToWinError(status) == 2):
            raise ctypes.WinError(api.LsaNtStatusToWinError(status))


def account_rights(sid):
    """Read the same target SID's actual lease immediately before registration."""
    with _account_policy(sid) as (api, policy, native_sid, UnicodeString):
        values = ctypes.POINTER(UnicodeString)()
        count = ctypes.c_uint32()
        api.LsaEnumerateAccountRights.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(UnicodeString)),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        api.LsaEnumerateAccountRights.restype = ctypes.c_uint32
        api.LsaFreeMemory.argtypes = [ctypes.c_void_p]
        status = api.LsaEnumerateAccountRights(
            policy, native_sid, ctypes.byref(values), ctypes.byref(count)
        )
        if status:
            code = api.LsaNtStatusToWinError(status)
            if code == 2:
                return []
            raise ctypes.WinError(code)
        try:
            return [
                ctypes.wstring_at(
                    values[index].buffer, values[index].length // ctypes.sizeof(ctypes.c_wchar)
                )
                for index in range(count.value)
            ]
        finally:
            api.LsaFreeMemory(values)
