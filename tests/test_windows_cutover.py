"""Receipt boundaries; installer/process qualification runs in windows-package."""

import hashlib
import json
import os
import sys

import pytest

from eom_email_watcher import windows_cutover
from eom_email_watcher.source_locks import SOURCE_LOCK_PROTOCOL, SourceProtocolUnavailable


def test_cutover_normalizes_paths_and_prepares_recovery_before_mutation():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    ).read_text()
    assert "ExpandEnvironmentVariables" in source, "Raw task paths still reach GetFullPath"
    pre = source[source.index("if ($Phase -eq 'Pre')") :]
    assert pre.index("Start-RestorationOwner") < pre.index("Disable-ScheduledTask")
    assert "$task.State -ne 'Disabled'" not in source
    assert ".GetTask($task.TaskName).Enabled" in source
    assert "Assert-OwnedImage" in source
    assert "failure-stage=" in source


def test_installer_catalog_follows_file_entries_including_new_programs():
    from windows_installer_template import catalog_macro

    template = """Section Install
  ; Copy main executable
  File "${MAINBINARYSRCPATH}"
  {{#each resources}}
    File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
  {{/each}}
  {{#each binaries}}
    File /a "/oname={{this}}" "{{no-escape @key}}"
  {{/each}}
  ; Create file associations
  File /a "/oname=additional-shipped-helper.exe" "helper.exe"
  WriteUninstaller "$INSTDIR\\uninstall.exe"
SectionEnd
"""
    macro = catalog_macro(template)
    assert "${MAINBINARYSRCPATH}" in macro
    assert "{{#each resources}}" in macro and "{{this.[1]}}" in macro
    assert "{{#each binaries}}" in macro and "{{this}}" in macro
    assert "additional-shipped-helper.exe" in macro
    assert "uninstall.exe" in macro
    assert "eom-mail-engine.exe" not in macro


@pytest.mark.skipif(os.name != "nt", reason="native Windows path/catalog/task boundaries")
def test_native_cutover_path_and_package_ownership_boundaries(tmp_path):
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    root = tmp_path / "installation"
    root.mkdir()
    shipped = root / "new-shipped-helper.exe"
    shipped.write_bytes(b"public package program")
    catalog = tmp_path / "package.txt"
    catalog.write_text("EW-PACKAGE-FILES-V1\n" + shipped.name + "\n", encoding="utf-16")
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$prefixSource=$source.Substring(0,$source.IndexOf('function Installed-Processes'))
. ([scriptblock]::Create($prefixSource)) -Phase Pre -InstallRoot $env:EW_ROOT `
    -ProgramCatalog $env:EW_CATALOG
$ownedPrograms=Load-OwnedPrograms
$env:EW_PUBLIC_ROOT=$env:EW_ROOT
$expected=Join-Path $env:EW_ROOT 'new-shipped-helper.exe'
foreach ($raw in @($expected,('  "'+$expected+'"  '),'"%EW_PUBLIC_ROOT%\new-shipped-helper.exe"')) {
    $p=Resolve-ExecutablePath $raw 'public matrix'
    if ($p -cne $expected -or !(Is-InstalledPath $p)) { throw 'Valid path rejected' }
    Assert-OwnedImage $p
}
foreach ($raw in @('', '"', 'C:\bad|path.exe', '%EW_MISSING_VARIABLE%\x.exe','relative.exe')) {
    if (Resolve-ExecutablePath $raw 'invalid matrix') { throw 'Invalid path accepted' }
}
if (Is-InstalledPath ($env:EW_ROOT+'-adjacent\x.exe')) { throw 'Adjacent root admitted' }
$unknowns=@((Join-Path $env:EW_ROOT 'unknown.exe'),
            (Join-Path $env:EW_ROOT 'sub\new-shipped-helper.exe'))
foreach ($p in $unknowns) {
    $rejected=$false
    try { Assert-OwnedImage $p } catch { $rejected=$true }
    if (!$rejected) { throw 'Unshipped program admitted' }
}
'PACKAGE_PATH_BOUNDARIES'
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=dict(os.environ, EW_SCRIPT=str(script), EW_ROOT=str(root), EW_CATALOG=str(catalog)),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PACKAGE_PATH_BOUNDARIES" in result.stdout.splitlines()


@pytest.mark.skipif(os.name != "nt", reason="native registered task enablement")
def test_native_task_enablement_does_not_use_stale_running_state(tmp_path):
    import subprocess
    import uuid
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$prefixSource=$source.Substring(0,$source.IndexOf('function Installed-Processes'))
. ([scriptblock]::Create($prefixSource)) -Phase Pre -InstallRoot $env:EW_ROOT
$arguments='-NoProfile -Command Start-Sleep -Seconds 60'
$a=New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments
$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(1)
Register-ScheduledTask -TaskName $env:EW_TASK -Action $a -Trigger $t | Out-Null
try {
    Start-ScheduledTask -TaskName $env:EW_TASK
    $deadline=[DateTime]::UtcNow.AddSeconds(20)
    do {
        $task=Get-ScheduledTask -TaskName $env:EW_TASK
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Task never ran' }
        Start-Sleep -Milliseconds 50
    } while ($task.State -ne 'Running')
    if (!(Task-Enabled $task)) { throw 'Enabled control rejected' }
    Disable-ScheduledTask -TaskName $env:EW_TASK | Out-Null
    # Keep the real pre-disable Running observation while querying registered settings.
    if (Task-Enabled $task) { throw 'Runtime state overrode disabled configuration' }
    'REGISTERED_ENABLED_PRESERVED'
} finally {
    Stop-ScheduledTask -TaskName $env:EW_TASK
    Unregister-ScheduledTask -TaskName $env:EW_TASK -Confirm:$false
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=dict(
            os.environ,
            EW_SCRIPT=str(script),
            EW_ROOT=str(tmp_path / "installation"),
            EW_TASK="EW-Enablement-" + uuid.uuid4().hex,
        ),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REGISTERED_ENABLED_PRESERVED" in result.stdout.splitlines()


def test_cutover_sets_trusted_compiler_context_before_add_type():
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    source = script.read_text()
    compiler = source.index("Add-Type -TypeDefinition")
    for statement in (
        "Set-Location -LiteralPath ([Environment]::SystemDirectory)",
        "[Environment]::CurrentDirectory = [Environment]::SystemDirectory",
    ):
        assert statement in source[:compiler], "Compiler still inherits the caller directory"


@pytest.mark.skipif(os.name != "nt", reason="native Windows compiler-directory reproduction")
def test_native_cutover_compiles_with_system_dll_in_caller_directory(tmp_path):
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    # Isolate the exact production compiler block, before touching real tasks.
    # The complete NSIS lane separately exercises the whole shipped script.
    (tmp_path / "System.dll").write_bytes(b"MZ public non-managed compiler-shadow fixture")
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$prefix=$source.Substring(0,$source.IndexOf('function Installed-Processes'))
& ([scriptblock]::Create($prefix)) -Phase Pre -InstallRoot $env:EW_ROOT
$process=[Diagnostics.Process]::GetCurrentProcess()
try {
    $image=[CutoverProcess]::Image($process.Handle)
    if (!$image.EndsWith('powershell.exe',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Native image query failed'
    }
    'TRUSTED_COMPILER_CONTEXT'
} finally { $process.Dispose() }
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        cwd=tmp_path,
        env=dict(os.environ, EW_SCRIPT=str(script), EW_ROOT=str(tmp_path / "installation")),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TRUSTED_COMPILER_CONTEXT" in result.stdout.splitlines()


@pytest.mark.parametrize("change", ["extra", "missing", "changed", "type", "empty"])
def test_payload_parity_rejects_unlisted_differences(change):
    from windows_cutover_parity import compare_manifests

    release = {
        "engine": {"type": "b", "sha256": "release"},
        "desktop": {"type": "b", "sha256": "same"},
    }
    test = {k: dict(v) for k, v in release.items()}
    test["engine"]["sha256"] = "qualification"
    if change == "extra":
        test["extra"] = {"type": "b", "sha256": "extra"}
    elif change == "missing":
        del test["desktop"]
    elif change == "changed":
        test["desktop"]["sha256"] = "wrong"
    elif change == "type":
        test["engine"]["type"] = "s"
    else:
        release, test = {}, {}
    with pytest.raises(ValueError):
        compare_manifests(release, test, {"engine"})


def test_payload_parity_accepts_only_expected_content_delta():
    from windows_cutover_parity import compare_manifests

    release = {
        "keyring": {"type": "x", "sha256": "release"},
        "module": {"type": "s", "sha256": "same"},
    }
    test = {
        "keyring": {"type": "x", "sha256": "qualification"},
        "module": {"type": "s", "sha256": "same"},
    }
    assert compare_manifests(release, test, {"keyring"}) == ["keyring"]
    assert compare_manifests(release, release, set()) == []
    with pytest.raises(ValueError):
        compare_manifests(release, release, {"keyring"})


def test_sidecar_parity_reads_real_archives_and_rejects_changed_module(tmp_path):
    from PyInstaller.archive.writers import CArchiveWriter
    from windows_cutover_parity import KEYRING, compare_manifests, sidecar_manifest

    def archive(name, keyring, module):
        ring_file, module_file = tmp_path / (name + ".ring"), tmp_path / (name + ".module")
        ring_file.write_bytes(keyring)
        module_file.write_bytes(module)
        path = tmp_path / (name + ".pkg")
        CArchiveWriter(
            str(path),
            [
                (KEYRING, str(ring_file), True, "x"),
                ("module", str(module_file), True, "x"),
                ("pyi-contents-directory _internal", None, False, "o"),
            ],
            "python313.dll",
        )
        return sidecar_manifest(path)

    release = archive("release", b"release authority", b"public module")
    test = archive("qualification", b"test authority", b"public module")
    assert compare_manifests(release, test, {KEYRING}) == [KEYRING]
    changed = archive("changed", b"test authority", b"changed module")
    with pytest.raises(ValueError, match="Unexpected payload differences"):
        compare_manifests(release, changed, {KEYRING})


def test_predecessor_actor_setup_reuses_seeded_account(tmp_path, monkeypatch):
    import windows_cutover_legacy_actor as actor

    from eom_email_watcher.db import Store

    database = tmp_path / "existing.db"
    # This probes fixture setup with real Store, not predecessor schema parity.
    # The native qualification freezes this same script against schema 34.
    monkeypatch.setattr(actor, "SCHEMA_VERSION", 34)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "actor",
            str(database),
            str(tmp_path / "ready"),
            str(tmp_path / "trigger"),
            "http://127.0.0.1:9",
            "seed",
        ],
    )
    actor.main()
    store = Store(database)
    before = store.mail_account("gmail", "public-fixture")
    actor.main()
    assert store.mail_account("gmail", "public-fixture") == before
    assert len([a for a in store.mail_accounts() if a.account_id == "public-fixture"]) == 1


def test_fixture_authority_signs_tests_and_is_rejected_for_release(tmp_path):
    from datetime import UTC, datetime, timedelta

    from build_desktop_sidecar import SidecarBuildError, validate_entitlement_keyring
    from connect_automate.entitlement import EntitlementDecision, EntitlementGate
    from windows_cutover_fixture import fixture_authority

    keyring, licence = fixture_authority(tmp_path)
    now = datetime.now(UTC)
    gate = EntitlementGate.for_test(licence, keyring.read_bytes(), now)
    assert gate.decision() is EntitlementDecision.ACTIVE
    assert gate.features_active(("connect.capability_exchange", "connect.automations"))
    expired = EntitlementGate.for_test(licence, keyring.read_bytes(), now + timedelta(days=2))
    assert expired.decision() is EntitlementDecision.EXPIRED
    missing = EntitlementGate.for_test(tmp_path / "missing", keyring.read_bytes(), now)
    assert missing.decision() is EntitlementDecision.MISSING
    with pytest.raises(SidecarBuildError, match="non-production key ID"):
        validate_entitlement_keyring(keyring)


def test_public_model_peer_exercises_real_analysis_client():
    import threading
    from datetime import UTC, datetime
    from http.server import ThreadingHTTPServer

    from windows_cutover_fixture import ModelHandler

    from eom_email_watcher.model import LocalModel

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LocalModel(
            f"http://127.0.0.1:{server.server_port}/v1",
            "cutover-fixture",
            timeout=5,
            require_auth=False,
        )
        assert client.health()[0]
        result = client.analyze(
            sender="vendor@example.test",
            subject="Public quote",
            received_at="2026-10-13T12:00:00+00:00",
            body="Promised public price is 100.",
            attachment_names=(),
            current_local_time=datetime(2026, 10, 13, 12, tzinfo=UTC),
        )
        assert result.category == "informational"
        assert result.summary == "Public cutover fixture analysed locally."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


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


@pytest.mark.skipif(os.name != "nt", reason="native Windows private evidence writer")
def test_native_evidence_writer_accepts_empty_installer_logs(tmp_path):
    from windows_cutover_fixture import write_private

    log = tmp_path / "installer.stdout"
    for content in (b"", b"public installer output", b""):
        write_private(log, content)
        assert log.read_bytes() == content


@pytest.mark.skipif(os.name != "nt", reason="native Windows global installer namespace")
def test_native_installer_mutex_is_root_keyed_in_global_kernel_namespace(tmp_path):
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    code = r"""
$ErrorActionPreference='Stop'
$name = & $env:EW_SCRIPT -Phase Name -InstallRoot $env:EW_ROOT
$same = & $env:EW_SCRIPT -Phase Name -InstallRoot ($env:EW_ROOT.ToUpperInvariant())
$other = & $env:EW_SCRIPT -Phase Name -InstallRoot ($env:EW_ROOT + '-other')
if ($name -cne $same -or $name -ceq $other) { throw 'Install identity is not canonical' }
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class KernelMutexName {
    [DllImport("ntdll.dll")]
    static extern int NtQueryObject(IntPtr h, int kind, IntPtr data, int size, ref int needed);
    public static string Read(IntPtr h) {
        int size=0; NtQueryObject(h,1,IntPtr.Zero,0,ref size);
        IntPtr buffer=Marshal.AllocHGlobal(size);
        try {
            if (NtQueryObject(h,1,buffer,size,ref size)!=0) throw new Exception("Query failed");
            return Marshal.PtrToStringUni(Marshal.ReadIntPtr(buffer,IntPtr.Size==8 ? 8:4),
                                         (ushort)Marshal.ReadInt16(buffer)/2);
        } finally { Marshal.FreeHGlobal(buffer); }
    }
}
'@
$m=[Threading.Mutex]::new($false,$name)
try {
    if (!$m.WaitOne(0)) { throw 'Fixture mutex busy' }
    $actual=[KernelMutexName]::Read($m.SafeWaitHandle.DangerousGetHandle())
    if (!$actual.StartsWith('\BaseNamedObjects\') -or $actual.Contains('\Sessions\')) {
        throw ('Session-local kernel object: ' + $actual)
    }
    $env:EW_MUTEX=$name
    $out = & powershell.exe -NoProfile -NonInteractive -Command '
        $m=[Threading.Mutex]::OpenExisting($env:EW_MUTEX)
        try { if ($m.WaitOne(0)) { $m.ReleaseMutex(); throw "Concurrent admission" }; "BUSY" }
        finally { $m.Dispose() }'
    if ($LASTEXITCODE -ne 0 -or $out -ne 'BUSY') { throw 'Concurrent admission was not excluded' }
    'GLOBAL_KERNEL_EXCLUSION'
} finally { $m.ReleaseMutex(); $m.Dispose() }
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=dict(os.environ, EW_SCRIPT=str(script), EW_ROOT=str(tmp_path / "installation")),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "GLOBAL_KERNEL_EXCLUSION"


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
    catalog = tmp_path / "package.txt"
    catalog.write_text("EW-PACKAGE-FILES-V1\neom-mail-engine.exe\n", encoding="utf-16")
    task = "EW-Cutover-Abort-" + uuid.uuid4().hex
    environment = dict(
        os.environ, EW_ROOT=str(root), EW_TASK=task, EW_SCRIPT=str(script), EW_CATALOG=str(catalog)
    )

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
        # The Pre child exits immediately after its first real disable. Recovery
        # must already have an owner, not one launched by a later statement.
        powershell(
            "$ErrorActionPreference='Stop'; "
            "$name= & $env:EW_SCRIPT -Phase Name -InstallRoot $env:EW_ROOT; "
            "$m=[Threading.Mutex]::new($false,$name); "
            "if (!$m.WaitOne(10000)) { throw 'Fixture installer busy' }; "
            "$env:EW_INSTALLER=$PID; "
            "$child='function Disable-ScheduledTask { param($TaskPath,$TaskName); "
            r"ScheduledTasks\Disable-ScheduledTask -TaskPath $TaskPath "
            "-TaskName $TaskName | Out-Null; "
            "exit 7 }; & $env:EW_SCRIPT -Phase Pre -InstallRoot $env:EW_ROOT "
            "-InstallerPid $env:EW_INSTALLER -ProgramCatalog $env:EW_CATALOG'; "
            "& powershell.exe -NoProfile -NonInteractive "
            "-ExecutionPolicy RemoteSigned -Command $child; "
            "if ($LASTEXITCODE -ne 7) { throw 'Pre did not abort at the first mutation' }; "
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
