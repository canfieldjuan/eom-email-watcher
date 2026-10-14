"""Native public process permission boundary, only for installer qualification."""

import ctypes
import json
import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


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


def run_standard_user_case(output, installer, engine, actor, run_powershell):
    """Run the entire refusal/retry fixture in a fresh non-admin user profile."""
    assert os.name == "nt", "standard-user qualification requires native Windows"
    root = Path(__file__).resolve().parents[1]
    name = "EWProbe-" + secrets.token_hex(4)
    password = "EW!a9" + secrets.token_urlsafe(24)
    values = dict(EW_PROBE_USER=name, EW_PROBE_PASSWORD=password)
    grants = []
    created = False
    try:
        run_powershell(
            "$ErrorActionPreference='Stop'; "
            "$secret=ConvertTo-SecureString $env:EW_PROBE_PASSWORD -AsPlainText -Force; "
            "New-LocalUser -Name $env:EW_PROBE_USER -Password $secret | Out-Null",
            **values,
        )
        created = True
        run_powershell(
            "Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $env:EW_PROBE_USER",
            EW_PROBE_USER=name,
        )
        # Grant only required inputs and this fixture's output, never all Users
        # or the runner profile. The disposable SID is removed in finally.
        for path, access in dict.fromkeys(
            [
                (root, "RX"),
                (Path(sys.base_prefix), "RX"),
                (Path(sys.prefix), "RX"),
                (output, "M"),
            ]
        ):
            grants.append(path)
            run_powershell(
                "$ErrorActionPreference='Stop'; "
                "& icacls.exe $env:EW_PROBE_PATH /grant "
                "($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER+':(OI)(CI)'+"
                "$env:EW_PROBE_ACCESS) /T /Q; "
                "if ($LASTEXITCODE -ne 0) { throw 'Fixture input ACL grant failed' }",
                EW_PROBE_USER=name,
                EW_PROBE_PATH=str(path),
                EW_PROBE_ACCESS=access,
            )
        stdout, stderr = output / "standard-user.stdout.log", output / "standard-user.stderr.log"
        command = [
            str(root / "scripts/prove_windows_cutover.py"),
            str(installer),
            str(engine),
            str(root),
            str(output),
            "--case",
            "old-then-new",
            "--actor",
            str(actor),
        ]
        run_powershell(
            "$ErrorActionPreference='Stop'; "
            "$secret=ConvertTo-SecureString $env:EW_PROBE_PASSWORD -AsPlainText -Force; "
            "$credential=New-Object System.Management.Automation.PSCredential "
            "($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER),$secret; "
            "Remove-Item Env:EW_PROBE_PASSWORD; "
            "$p=Start-Process -FilePath $env:EW_PROBE_PYTHON -ArgumentList $env:EW_PROBE_ARGS "
            "-Credential $credential -LoadUserProfile -WorkingDirectory $env:EW_PROBE_OUTPUT "
            "-RedirectStandardOutput $env:EW_PROBE_STDOUT "
            "-RedirectStandardError $env:EW_PROBE_STDERR "
            "-PassThru; $handle=$p.Handle; "
            "if (-not $p.WaitForExit(600000)) { "
            "& taskkill.exe /PID $p.Id /T /F | Out-Null; throw 'Standard-user fixture timeout' }; "
            "$p.Refresh(); if ($p.ExitCode -ne 0) { "
            "throw ('Standard-user fixture exit='+$p.ExitCode+'; '+"
            "(Get-Content -LiteralPath $env:EW_PROBE_STDERR -Raw)) }",
            **values,
            timeout=700,
            EW_PROBE_PYTHON=sys.executable,
            EW_PROBE_ARGS=subprocess.list2cmdline(command),
            EW_PROBE_OUTPUT=str(output),
            EW_PROBE_STDOUT=str(stdout),
            EW_PROBE_STDERR=str(stderr),
        )
        result = json.loads((output / "standard-user-result.json").read_text())
        assert result["case"] == "old-then-new" and result["result"] == "PASS", result
        return result
    finally:
        for path in reversed(grants):
            run_powershell(
                "$ErrorActionPreference='Stop'; "
                "& icacls.exe $env:EW_PROBE_PATH /remove:g "
                "($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER) /T /Q; "
                "if ($LASTEXITCODE -ne 0) { throw 'Fixture ACL removal failed' }",
                EW_PROBE_USER=name,
                EW_PROBE_PATH=str(path),
            )
        if created:
            run_powershell(
                "Remove-LocalUser -Name $env:EW_PROBE_USER -ErrorAction Stop", EW_PROBE_USER=name
            )
