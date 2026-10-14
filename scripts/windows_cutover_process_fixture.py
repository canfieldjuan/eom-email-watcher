"""Native public process permission boundary, only for installer qualification."""

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from windows_batch_logon import set_batch_logon_right


class ScheduledActor:
    """Hold the actual scheduled actor's process object, not a later PID lookup."""

    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def settle(self):
        try:
            if self.kernel.WaitForSingleObject(self.handle, 10000) != 0:
                raise RuntimeError("Scheduled actor termination is unproven")
        finally:
            self.kernel.CloseHandle(self.handle)


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


def fixture_payloads(output, installer, engine, actor):
    """Give the case private SHA-identical inputs without granting build trees."""
    destination = output / "standard-inputs"
    destination.mkdir(mode=0o700)
    copies, hashes = [], {}
    for name, original in (
        ("installer.exe", installer),
        ("engine.exe", engine),
        ("actor.exe", actor),
    ):
        copied = destination / name
        shutil.copy2(original, copied)
        original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
        copied_sha = hashlib.sha256(copied.read_bytes()).hexdigest()
        assert original_sha == copied_sha, "Fixture input copy changed source bytes"
        hashes[name] = {"source_sha256": original_sha, "copied_sha256": copied_sha}
        copies.append(copied)
    (output / "standard-inputs.json").write_text(json.dumps(hashes, indent=2))
    return copies


def fixture_access_paths(root, output):
    return list(
        dict.fromkeys(
            [
                (root / "scripts", "RX"),
                (root / "src", "RX"),
                (root / "desktop/src-tauri/windows", "RX"),
                (Path(sys.base_prefix), "RX"),
                (Path(sys.prefix), "RX"),
                (output, "M"),
            ]
        )
    )


def cleanup_admitted(receipt, account):
    return (
        isinstance(receipt, dict)
        and receipt.get("account") == account
        and receipt.get("quiesced") is True
        and receipt.get("phase") in ("not-started", "exited", "timeout-quiesced")
    )


def disposable_user_sid(name, run_powershell):
    return run_powershell(
        "(Get-LocalUser -Name $env:EW_PROBE_USER -ErrorAction Stop).SID.Value", EW_PROBE_USER=name
    )


def run_standard_user_case(
    output, installer, engine, actor, run_powershell, *, timeout_probe=False, orphan_probe=False
):
    """Own identity, bounded input permissions, native user environment and cleanup."""
    assert os.name == "nt", "standard-user qualification requires native Windows"
    root = Path(__file__).resolve().parents[1]
    installer, engine, actor = fixture_payloads(output, installer, engine, actor)
    tool = shutil.which("7z")
    assert tool, "Installer payload comparison requires the CI 7z executable"
    name = "EWProbe-" + secrets.token_hex(4)
    password = "EW!a9" + secrets.token_urlsafe(24)
    values = dict(EW_PROBE_USER=name, EW_PROBE_PASSWORD=password)
    grants, cleanup_errors, timings = [], [], []
    created = False
    sid, batch_leased, launch_attempted, process_state = None, False, False, None
    lease_timings = []
    primary = None
    result = None

    def acl(path, access=None):
        started = time.monotonic()
        try:
            run_powershell(
                "$ErrorActionPreference='Stop'; "
                "& icacls.exe $env:EW_PROBE_PATH "
                + (
                    "/grant ($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER+':(OI)(CI)'+"
                    "$env:EW_PROBE_ACCESS) /T /Q; "
                    if access
                    else "/remove:g ($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER) /T /Q; "
                )
                + "if ($LASTEXITCODE -ne 0) { throw 'Fixture ACL operation failed' }",
                timeout=300,
                EW_PROBE_USER=name,
                EW_PROBE_PATH=str(path),
                EW_PROBE_ACCESS=access or "",
            )
        finally:
            timings.append(
                {
                    "operation": "grant" if access else "remove",
                    "path": str(path),
                    "seconds": time.monotonic() - started,
                    "timeout_seconds": 300,
                }
            )

    try:
        run_powershell(
            "$ErrorActionPreference='Stop'; "
            "$secret=ConvertTo-SecureString $env:EW_PROBE_PASSWORD -AsPlainText -Force; "
            "New-LocalUser -Name $env:EW_PROBE_USER -Password $secret | Out-Null",
            **values,
        )
        created = True
        sid = disposable_user_sid(name, run_powershell)
        batch_leased = True  # Include a partially completed LSA grant in cleanup.
        started = time.monotonic()
        try:
            set_batch_logon_right(sid, True)
        finally:
            lease_timings.append(
                {"operation": "grant", "sid": sid, "seconds": time.monotonic() - started}
            )
        run_powershell(
            "$ErrorActionPreference='Stop'; "
            "$group=Get-LocalGroup -SID 'S-1-5-32-545'; "
            "if (-not (Get-LocalGroupMember -Group $group | Where-Object "
            "{ $_.Name -eq ($env:COMPUTERNAME+'\\'+$env:EW_PROBE_USER) })) "
            "{ Add-LocalGroupMember -Group $group -Member $env:EW_PROBE_USER }",
            EW_PROBE_USER=name,
        )
        for path, access in fixture_access_paths(root, output):
            grants.append(path)  # Include partial recursive grants in cleanup.
            acl(path, access)
        command = [
            sys.executable,
            str(root / "scripts/prove_windows_cutover.py"),
            str(installer),
            str(engine),
            str(root),
            str(output),
            "--case",
            "old-then-new",
            "--actor",
            str(actor),
            "--tool-directory",
            str(Path(tool).parent),
        ]
        if timeout_probe:
            command.append("--timeout-probe")
        if orphan_probe:
            command.append("--orphan-probe")
        launch_attempted = True
        run_powershell(
            "& $env:EW_PROBE_LAUNCHER",
            **values,
            timeout=700,
            EW_PROBE_PYTHON=sys.executable,
            EW_PROBE_ARGS=subprocess.list2cmdline(command),
            EW_PROBE_OUTPUT=str(output),
            EW_PROBE_LAUNCHER=str(root / "scripts/windows_standard_user.ps1"),
            EW_PROBE_WAIT_MS="10000" if timeout_probe else "600000",
        )
        result = json.loads((output / "standard-user-result.json").read_text())
        assert result["case"] == "old-then-new" and result["result"] == "PASS", result
    except Exception as error:
        primary = error
    finally:
        admitted = not launch_attempted
        if launch_attempted:
            try:
                process_state = json.loads(
                    (output / "native-process-state.json").read_text(encoding="utf-8-sig")
                )
                admitted = cleanup_admitted(process_state, name)
            except (OSError, ValueError) as error:
                cleanup_errors.append(error)
        if not admitted:
            cleanup_errors.append(
                RuntimeError("Case quiescence unproven; identity, right and ACLs retained")
            )
        else:
            for path in reversed(grants):
                try:
                    acl(path)
                except Exception as error:
                    cleanup_errors.append(error)
            if batch_leased:
                started = time.monotonic()
                try:
                    set_batch_logon_right(sid, False)
                except Exception as error:
                    cleanup_errors.append(error)
                finally:
                    lease_timings.append(
                        {"operation": "revoke", "sid": sid, "seconds": time.monotonic() - started}
                    )
            if created:
                try:
                    run_powershell(
                        "Remove-LocalUser -Name $env:EW_PROBE_USER -ErrorAction Stop",
                        EW_PROBE_USER=name,
                    )
                except Exception as error:
                    cleanup_errors.append(error)
        (output / "standard-user-cleanup.json").write_text(
            json.dumps(
                {
                    "primary_error": str(primary) if primary else None,
                    "cleanup_errors": [str(error) for error in cleanup_errors],
                    "acl_timings": timings,
                    "batch_logon_lease": lease_timings,
                    "process_state": process_state,
                    "resources_retained": not admitted,
                },
                indent=2,
            )
        )
    if cleanup_errors:
        raise ExceptionGroup(
            "Standard-user fixture and cleanup failed",
            ([primary] if primary else []) + cleanup_errors,
        )
    if primary is not None:
        raise primary
    return result
