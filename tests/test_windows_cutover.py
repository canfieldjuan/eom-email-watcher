"""Receipt boundaries; installer/process qualification runs in windows-package."""

import hashlib
import json
import os
import sys

import pytest

from eom_email_watcher import windows_cutover
from eom_email_watcher.source_locks import SOURCE_LOCK_PROTOCOL, SourceProtocolUnavailable


@pytest.mark.parametrize(
    "change",
    [{}, {"version": True}, {"protocol": "other"}, {"engine_sha256": "0" * 64}],
)
def test_receipt_binds_exact_engine_and_protocol(tmp_path, monkeypatch, change):
    binary = tmp_path / "engine.exe"
    binary.write_bytes(b"public-engine-fixture")
    monkeypatch.setattr(sys, "executable", str(binary))
    record = {
        "version": 1,
        "protocol": SOURCE_LOCK_PROTOCOL,
        "engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    record.update(change)
    monkeypatch.setattr(windows_cutover, "_read_receipt", lambda _: json.dumps(record).encode())
    if change:
        with pytest.raises(SourceProtocolUnavailable, match="installer update"):
            windows_cutover.verify_installation()
    else:
        windows_cutover.verify_installation()
        binary.write_bytes(b"replacement-engine-fixture")
        with pytest.raises(SourceProtocolUnavailable, match="installer update"):
            windows_cutover.verify_installation()


@pytest.mark.skipif(os.name != "nt", reason="native Windows regular-file receipt reader")
def test_native_receipt_missing_oversized_malformed_and_valid(tmp_path, monkeypatch):
    binary = tmp_path / "engine.exe"
    binary.write_bytes(b"public-engine-fixture")
    monkeypatch.setattr(sys, "executable", str(binary))
    receipt = tmp_path / windows_cutover.RECEIPT_NAME
    for value in (None, b"x" * 4097, b'{"version":'):
        if value is not None:
            receipt.write_bytes(value)
        with pytest.raises(SourceProtocolUnavailable, match="installer update"):
            windows_cutover.verify_installation()
    receipt.write_text(
        json.dumps(
            {
                "version": 1,
                "protocol": SOURCE_LOCK_PROTOCOL,
                "engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8-sig",
    )
    windows_cutover.verify_installation()


@pytest.mark.skipif(os.name != "nt", reason="native Windows scheduled-task installer restoration")
def test_native_preinstall_abort_restores_scheduled_task(tmp_path):
    import subprocess
    import time
    import uuid
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    root = tmp_path / "installation"
    root.mkdir()
    task = "EW-Cutover-Abort-" + uuid.uuid4().hex
    environment = dict(os.environ, EW_ROOT=str(root), EW_TASK=task, EW_SCRIPT=str(script))

    def powershell(code, *, expected=0):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
            env=environment,
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result.stdout.strip()

    powershell(
        "$ErrorActionPreference='Stop'; "
        "$a=New-ScheduledTaskAction -Execute (Join-Path $env:EW_ROOT 'eom-mail-engine.exe'); "
        "$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(1); "
        "Register-ScheduledTask -TaskName $env:EW_TASK -Action $a -Trigger $t | Out-Null"
    )
    try:
        # Real process lifetime and native named mutex, not a mocked platform:
        # the installer exits after Pre, so its restoration worker must recover.
        powershell(
            "$ErrorActionPreference='Stop'; "
            "$m=[Threading.Mutex]::new($false,'Local\\EomEmailWatcherSourceCutoverV1'); "
            "if (!$m.WaitOne(10000)) { throw 'Fixture installer busy' }; "
            "& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned "
            "-File $env:EW_SCRIPT -Phase Pre -InstallRoot $env:EW_ROOT -InstallerPid $PID; "
            "if ($LASTEXITCODE -ne 0) { throw 'Pre failed' }; "
            "if ((Get-ScheduledTask -TaskName $env:EW_TASK).State -ne 'Disabled') "
            "{ throw 'Pre did not disable task' }; exit 7",
            expected=7,
        )
        deadline = time.monotonic() + 30
        pending = root / "windows-cutover-pending-v1.json"
        while pending.exists():
            assert time.monotonic() < deadline, "Aborted installer did not restore task"
            time.sleep(0.05)
        assert powershell("(Get-ScheduledTask -TaskName $env:EW_TASK).State") == "Ready"
        assert not (root / windows_cutover.RECEIPT_NAME).exists()
    finally:
        powershell("Unregister-ScheduledTask -TaskName $env:EW_TASK -Confirm:$false")
