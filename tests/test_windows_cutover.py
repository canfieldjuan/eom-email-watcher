"""Receipt boundaries; installer/process qualification runs in windows-package."""

import hashlib
import io
import json
import os
import struct
import sys
import warnings
import zipfile
import zlib

import pytest
from windows_qualification_environment import encoded_powershell_script, powershell_environment

from eom_email_watcher import windows_cutover
from eom_email_watcher.source_locks import SOURCE_LOCK_PROTOCOL, SourceProtocolUnavailable


@pytest.mark.skipif(os.name != "nt", reason="actual PowerShell process getter exception semantics")
@pytest.mark.parametrize("native_error", [0, 5, 6, 87])
def test_native_product_handle_open_preserves_win32_error(tmp_path, native_error):
    from pathlib import Path

    import prove_windows_cutover as proof

    # Invoke the actual product getter assignments, with a public throwing CLR
    # getter. This also exercises the old assignments before the owner fix.
    code = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(
    $env:EW_PRODUCT_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Product script does not parse' }
$owner=@($ast.FindAll({param($n)
    $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Open-CutoverProcessHandle'
},$true))
foreach ($function in $owner) { . ([scriptblock]::Create($function.Extent.Text)) }
$assignments=@($ast.FindAll({param($n)
    $n -is [Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$handle' -and
    ($n.Right.Extent.Text -match 'Open-CutoverProcessHandle|\$(live|installer)\.Handle')
},$true))
if ($assignments.Count -ne 2) { throw 'Process handle readers changed without this proof' }
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
public class PublicHandleProbe {
    public int Error;
    public IntPtr Handle {
        get {
            if (Error != 0) throw new Win32Exception(Error);
            return new IntPtr(1234);
        }
    }
}
'@
$expected=[int]$env:EW_NATIVE_ERROR
$probe=New-Object PublicHandleProbe;$probe.Error=$expected
$legacy=$probe.Handle
if ($expected -ne 0 -and $null -ne $legacy) { throw 'Frozen property control did not suppress' }
foreach ($assignment in $assignments) {
    $live=$probe;$installer=$probe;$handle=$null;$caught=$false
    try {
        . ([scriptblock]::Create($assignment.Extent.Text))
        if ($expected -ne 0) { throw 'Handle denial was converted to a null handle' }
        if ($handle -ne [IntPtr]::new(1234)) { throw 'Valid handle was changed' }
    } catch {
        $base=$_.Exception.GetBaseException()
        if ($expected -eq 0 -or $base -isnot [ComponentModel.Win32Exception] -or
            $base.NativeErrorCode -ne $expected) { throw }
        $caught=$true
    }
    if ($caught -ne ($expected -ne 0)) { throw 'Handle result did not preserve the error' }
}
'HANDLE_OWNER_BOUNDARIES_PASS'
'''
    output = proof.probe_output(f"product-handle-error-{native_error}", tmp_path)
    result = proof.powershell(
        code, EW_PRODUCT_SOURCE=str(
            Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
        ), EW_NATIVE_ERROR=str(native_error),
    )
    (output / "handle-owner.txt").write_text(result)
    assert "HANDLE_OWNER_BOUNDARIES_PASS" in result.splitlines()


def test_restore_opens_handle_before_identity_check():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    ).read_text()
    restore = source[source.index("if ($Phase -eq 'Restore')") :]
    assert restore.index("Open-CutoverProcessHandle $installer") < restore.index(
        "$installer.StartTime"
    ), "Restore validates identity before opening its persistent handle"


def test_restore_tasks_contains_per_task_failure_boundary():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    ).read_text()
    owner = source.split("function Restore-Tasks {", 1)[1].split("if ($Phase -eq 'Restore')", 1)[0]
    assert "foreach ($saved in @(Get-Content" not in owner, (
        "JSON array is wrapped as one saved task in Windows PowerShell"
    )
    assert "catch" in owner, "First restoration failure aborts the remaining saved tasks"
    assert owner.index("catch") < owner.index("Remove-Item -LiteralPath $pending")


@pytest.mark.skipif(os.name != "nt", reason="actual PowerShell restoration loop")
def test_native_restore_attempts_all_tasks_and_keeps_pending_on_failure(tmp_path):
    from pathlib import Path

    import prove_windows_cutover as proof

    code = r'''
$ErrorActionPreference='Stop'
$pending=Join-Path $env:EW_RESTORE_ROOT 'pending.json'
$tasks=@(
    @{TaskPath='\';TaskName='bad-first';Enabled=$true},
    @{TaskPath='\';TaskName='good';Enabled=$true},
    @{TaskPath='\';TaskName='bad-last';Enabled=$true},
    @{TaskPath='\';TaskName='disabled';Enabled=$false}
)
$script:attempts=[Collections.Generic.List[string]]::new()
$script:fail=$true
$script:disabledRequested=$false
$script:enabled=@{'bad-first'=$false;'good'=$false;'bad-last'=$false;'disabled'=$false}
function Enable-ScheduledTask {
    param($TaskPath,$TaskName)
    $names=@($TaskName)
    $script:attempts.Add(($names -join ','))
    if ($names -contains 'disabled') { $script:disabledRequested=$true }
    if ($names.Count -ne 1) { throw 'Restore passed a task collection instead of one task' }
    if ($script:fail -and $TaskName.StartsWith('bad-')) { throw ($TaskName+' denied') }
    $script:enabled[$TaskName]=$true
}
# Exact old 85d96bb owner: prove its array/disabled dispatch defect in PS5.1.
function Restore-Tasks {
    if (Test-Path -LiteralPath $pending) {
        $failures = @()
        foreach ($saved in @(Get-Content -LiteralPath $pending -Raw | ConvertFrom-Json)) {
            try {
                if ($saved.Enabled) {
                    Enable-ScheduledTask -TaskPath $saved.TaskPath -TaskName $saved.TaskName |
                        Out-Null
                }
            } catch {
                $failures += ($saved.TaskPath + $saved.TaskName + ': ' + $_.Exception.Message)
            }
        }
        if ($failures.Count) { throw ('Task restoration failed: ' + ($failures -join '; ')) }
        Remove-Item -LiteralPath $pending
    }
}
ConvertTo-Json -InputObject $tasks | Set-Content -LiteralPath $pending
$failure=$null
try { Restore-Tasks } catch { $failure=$_.Exception.Message }
if (!$failure -or $failure -notmatch 'Restore passed a task collection instead of one task' -or
    $attempts.Count -ne 1 -or $attempts[0] -ne 'bad-first,good,bad-last,disabled' -or
    !$disabledRequested -or !(Test-Path -LiteralPath $pending)) {
    throw 'Frozen array/disabled-dispatch control did not reproduce'
}
'FROZEN_SAVED_DISABLED_REQUEST_FAIL_PROVEN'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:EW_PRODUCT_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Product script did not parse' }
$owner=$ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Restore-Tasks'
},$true)
if (!$owner) { throw 'Restoration owner missing' }
. ([scriptblock]::Create($owner.Extent.Text))
$attempts.Clear();$failure=$null;$script:disabledRequested=$false
try { Restore-Tasks } catch { $failure=$_.Exception.Message }
if (!$failure -or $failure -notmatch 'bad-first denied' -or $failure -notmatch 'bad-last denied') {
    throw 'Restoration did not aggregate both failures'
}
if (($attempts -join ',') -ne 'bad-first,good,bad-last' -or !(Test-Path -LiteralPath $pending)) {
    throw 'Restoration skipped a saved task or discarded the pending file'
}
if ($disabledRequested -or $enabled['disabled']) { throw 'Saved-disabled task was enabled' }
$script:fail=$false;$attempts.Clear()
Restore-Tasks
if (($attempts -join ',') -ne 'bad-first,good,bad-last' -or (Test-Path -LiteralPath $pending)) {
    throw 'Successful retry did not finish every saved task'
}
if ($disabledRequested -or $enabled['disabled']) { throw 'Retry enabled a saved-disabled task' }
foreach ($name in 'bad-first','good','bad-last') {
    if (!$enabled[$name]) { throw ('Saved-enabled task was not restored: '+$name) }
}
# The existing writer can serialize one saved task as a single JSON object.
foreach ($wasEnabled in $true,$false) {
    $script:enabled['single']=$false;$attempts.Clear()
    @{TaskPath='\';TaskName='single';Enabled=$wasEnabled} |
        ConvertTo-Json | Set-Content -LiteralPath $pending
    Restore-Tasks
    if ($enabled['single'] -ne $wasEnabled -or $attempts.Count -ne [int]$wasEnabled -or
        (Test-Path -LiteralPath $pending)) { throw 'Single-task restoration changed saved state' }
}
'SAVED_DISABLED_STAYS_DISABLED_PASS'
'RESTORE_ALL_AND_RETRY_PASS'
'''
    output = proof.probe_output("restore-all-tasks", tmp_path)
    result = proof.powershell(
        code,
        EW_RESTORE_ROOT=str(output),
        EW_PRODUCT_SOURCE=str(
            Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
        ),
    )
    assert "FROZEN_SAVED_DISABLED_REQUEST_FAIL_PROVEN" in result.splitlines()
    assert "SAVED_DISABLED_STAYS_DISABLED_PASS" in result.splitlines()
    assert "RESTORE_ALL_AND_RETRY_PASS" in result.splitlines()


@pytest.mark.skipif(os.name != "nt", reason="actual product Restore AST with public CLR process")
@pytest.mark.parametrize("mode", ["match", "mismatch", "denied"])
def test_native_restore_identity_uses_opened_handle(tmp_path, mode):
    from pathlib import Path

    import prove_windows_cutover as proof

    code = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(
    $env:EW_PRODUCT_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Product script does not parse' }
$owner=$ast.Find({param($n)
    $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Open-CutoverProcessHandle'
},$true)
. ([scriptblock]::Create($owner.Extent.Text))
$restore=$ast.Find({param($n)
    $n -is [Management.Automation.Language.IfStatementAst] -and
    $n.Clauses[0].Item1.Extent.Text -eq "$('$')Phase -eq 'Restore'"
},$true)
if (!$restore) { throw 'Restore owner missing' }
$entry=($restore.Clauses[0].Item2.Statements | Select-Object -First 2 |
    ForEach-Object { $_.Extent.Text }) -join "`n"
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
public class PublicRestoreProcess {
    public string Mode; public bool Held; public bool Waited;
    public IntPtr Handle { get {
        if (Mode == "denied") throw new Win32Exception(5);
        Held=true; return new IntPtr(1234);
    } }
    public DateTime StartTime { get {
        if (!Held) throw new InvalidOperationException("Identity read before stable handle");
        return new DateTime(Mode == "mismatch" ? 101 : 100, DateTimeKind.Utc);
    } }
    public void WaitForExit() {
        if (!Held) throw new InvalidOperationException("Wait without stable handle");
        Waited=true;
    }
}
'@
$probe=New-Object PublicRestoreProcess;$probe.Mode=$env:EW_RESTORE_MODE
function Get-Process { param($Id,$ErrorAction) return $probe }
$InstallerPid=1234;$InstallerStarted=100;$restoreReady=$env:EW_RESTORE_READY
$caught=$false
try { . ([scriptblock]::Create($entry)) }
catch {
    $base=$_.Exception.GetBaseException()
    if ($probe.Mode -ne 'denied' -or $base -isnot [ComponentModel.Win32Exception] -or
        $base.NativeErrorCode -ne 5) { throw }
    $caught=$true
}
$ready=Test-Path -LiteralPath $restoreReady
if ($probe.Mode -eq 'match') {
    if (!$probe.Held -or !$probe.Waited -or !$ready -or $caught) {
        throw 'Installer not recognized'
    }
} elseif ($ready -or $probe.Waited -or ($caught -ne ($probe.Mode -eq 'denied'))) {
    throw 'Unauthenticated installer became ready or waited'
}
'RESTORE_IDENTITY_BOUNDARIES_PASS'
'''
    output = proof.probe_output(f"restore-identity-{mode}", tmp_path)
    result = proof.powershell(
        code,
        EW_PRODUCT_SOURCE=str(
            Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
        ),
        EW_RESTORE_MODE=mode, EW_RESTORE_READY=str(output / "restore.ready"),
    )
    (output / "restore-identity.txt").write_text(result)
    assert "RESTORE_IDENTITY_BOUNDARIES_PASS" in result.splitlines()


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
        env=powershell_environment(
            dict(os.environ, EW_SCRIPT=str(script), EW_ROOT=str(root), EW_CATALOG=str(catalog))
        ),
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
        env=powershell_environment(
            dict(
                os.environ,
                EW_SCRIPT=str(script),
                EW_ROOT=str(tmp_path / "installation"),
                EW_TASK="EW-Enablement-" + uuid.uuid4().hex,
            )
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
        env=powershell_environment(
            dict(os.environ, EW_SCRIPT=str(script), EW_ROOT=str(tmp_path / "installation"))
        ),
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


def _stdlib_zip(members, *, date=(2020, 1, 2, 3, 4, 6), metadata=None, comment=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.comment = comment
        for name, content in members:
            info = zipfile.ZipInfo(name, date)
            if metadata is not None:
                metadata(info)
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
                archive.writestr(info, content)
    return output.getvalue()


def _sidecar_with_zip(tmp_path, label, content, *, member_name="base_library.zip"):
    from PyInstaller.archive.writers import CArchiveWriter
    from windows_cutover_parity import KEYRING, sidecar_manifest

    payload, ring = tmp_path / (label + ".zip"), tmp_path / (label + ".ring")
    payload.write_bytes(content)
    ring.write_bytes(label.encode())
    binary = tmp_path / (label + ".pkg")
    CArchiveWriter(
        str(binary),
        [(member_name, str(payload), True, "x"), (KEYRING, str(ring), True, "x")],
        "python313.dll",
    )
    return sidecar_manifest(binary)


def test_base_library_parity_accepts_reordered_members(tmp_path):
    from windows_cutover_parity import KEYRING, compare_manifests

    members = [("alpha.pyc", b"plumless"), ("beta.pyc", b"beta")]
    first, second = _stdlib_zip(members), _stdlib_zip(list(reversed(members)))
    assert first != second
    release = _sidecar_with_zip(tmp_path, "release", first)
    qualified = _sidecar_with_zip(tmp_path, "qualification", second)
    assert compare_manifests(release, qualified, {KEYRING}) == [KEYRING]


@pytest.mark.parametrize(
    "change",
    ["name", "missing", "extra", "size", "sha256", "date", "comment", "extra_field",
     "permissions", "compression", "archive_comment"],
)
def test_base_library_parity_rejects_member_changes(tmp_path, change):
    from windows_cutover_parity import KEYRING, compare_manifests

    members = [("alpha.pyc", b"plumless"), ("beta.pyc", b"beta")]
    changed = list(members)
    options = {}
    if change == "name":
        changed[0] = ("renamed.pyc", changed[0][1])
    elif change == "missing":
        changed.pop()
    elif change == "extra":
        changed.append(("extra.pyc", b"extra"))
    elif change == "size":
        changed[0] = (changed[0][0], b"longer content")
    elif change == "sha256":
        assert len(b"plumless") == len(b"buckeroo")
        assert zlib.crc32(b"plumless") == zlib.crc32(b"buckeroo")
        changed[0] = (changed[0][0], b"buckeroo")
    elif change == "date":
        options["date"] = (2022, 1, 2, 3, 4, 6)
    elif change == "archive_comment":
        options["comment"] = b"changed archive comment"
    else:
        attribute, value = {
            "comment": ("comment", b"changed member comment"),
            "extra_field": ("extra", b"\xfe\xca\x01\x00x"),
            "permissions": ("external_attr", 0o100700 << 16),
            "compression": ("compress_type", zipfile.ZIP_DEFLATED),
        }[change]
        options["metadata"] = lambda info: setattr(info, attribute, value)
    release = _sidecar_with_zip(tmp_path, "release", _stdlib_zip(members))
    qualified = _sidecar_with_zip(tmp_path, "qualification", _stdlib_zip(changed, **options))
    with pytest.raises(ValueError, match="Unexpected payload differences"):
        compare_manifests(release, qualified, {KEYRING})


@pytest.mark.parametrize(
    "damage", ["duplicate", "not_zip", "truncated", "crc", "encrypted", "unsupported"]
)
def test_base_library_parity_rejects_invalid_archives(tmp_path, damage):
    members = [("alpha.pyc", b"plumless")]
    if damage == "duplicate":
        content = _stdlib_zip(members + members)
    elif damage == "not_zip":
        content = b"not a ZIP archive"
    elif damage == "truncated":
        content = _stdlib_zip(members)[:-12]
    elif damage == "crc":
        content = _stdlib_zip(members).replace(b"plumless", b"plumlesz")
    else:
        content = bytearray(_stdlib_zip(members))
        central = content.index(b"PK\x01\x02")
        local_offset, central_offset, value = (
            (6, 8, 1) if damage == "encrypted" else (8, 10, 99)
        )
        struct.pack_into("<H", content, local_offset, value)
        struct.pack_into("<H", content, central + central_offset, value)
    with pytest.raises(ValueError, match="base_library.zip"):
        _sidecar_with_zip(tmp_path, "invalid", content)


@pytest.mark.parametrize(
    "member_name",
    ["nested/base_library.zip", "base_library.zip.bak", "BASE_LIBRARY.ZIP", "other.zip"],
)
def test_base_library_normalization_is_exact_entry_only(tmp_path, member_name):
    from windows_cutover_parity import KEYRING, compare_manifests

    members = [("alpha.pyc", b"a"), ("beta.pyc", b"b")]
    release = _sidecar_with_zip(tmp_path, "release", _stdlib_zip(members), member_name=member_name)
    qualified = _sidecar_with_zip(
        tmp_path, "qualification", _stdlib_zip(list(reversed(members))), member_name=member_name
    )
    with pytest.raises(ValueError, match="Unexpected payload differences"):
        compare_manifests(release, qualified, {KEYRING})


@pytest.mark.parametrize("members", [[], [("one.pyc", b"one")]])
def test_base_library_parity_accepts_unchanged_boundary_archives(tmp_path, members):
    from windows_cutover_parity import KEYRING, compare_manifests

    content = _stdlib_zip(members)
    release = _sidecar_with_zip(tmp_path, "release", content)
    qualified = _sidecar_with_zip(tmp_path, "qualification", content)
    assert compare_manifests(release, qualified, {KEYRING}) == [KEYRING]


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


def test_fixture_authority_signs_tests_and_is_rejected_for_release(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from build_desktop_sidecar import SidecarBuildError, validate_entitlement_keyring
    from connect_automate.entitlement import EntitlementDecision, EntitlementGate
    from windows_cutover_fixture import fixture_authority

    # Native archive routing moves this fixture outside the runner's profile.
    # Give the real verifier an explicit isolated root, as the full proof does;
    # retain its ACL/ancestor/bounds checks and never use the host licence.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
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


@pytest.fixture
def mail_startup(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import windows_cutover_fixture as fixture

    jar = b"public startup fixture"
    (tmp_path / "greenmail.jar").write_bytes(jar)
    monkeypatch.setattr(fixture, "GREENMAIL_SHA256", hashlib.sha256(jar).hexdigest())
    state = SimpleNamespace(
        clock=0.0,
        argv=[],
        terminated=False,
        waited=False,
        attempts=0,
        startup_log="",
        exit_code=None,
        ready_at=3.0,
        folders=[],
        error=fixture.imaplib.IMAP4.error("LOGIN failed"),
    )

    def launch(argv, *, stdout, stderr):
        state.argv = argv
        stdout.write(state.startup_log)
        stdout.flush()
        return SimpleNamespace(
            poll=lambda: state.exit_code,
            terminate=lambda: setattr(state, "terminated", True),
            wait=lambda timeout: setattr(state, "waited", True),
        )

    monkeypatch.setattr(
        fixture,
        "subprocess",
        SimpleNamespace(
            run=lambda *a, **kw: SimpleNamespace(stdout="public java", stderr=""),
            Popen=launch,
            STDOUT=fixture.subprocess.STDOUT,
        ),
    )

    @contextmanager
    def probe():
        yield SimpleNamespace(bind=lambda address: None)

    monkeypatch.setattr(fixture, "socket", SimpleNamespace(socket=probe))
    monkeypatch.setattr(
        fixture,
        "time",
        SimpleNamespace(
            monotonic=lambda: state.clock,
            sleep=lambda seconds: setattr(state, "clock", state.clock + seconds),
        ),
    )

    @contextmanager
    def connection(self):
        state.attempts += 1
        if state.clock < state.ready_at:
            raise state.error

        def create(folder):
            state.folders.append(folder)
            return ("OK", [])

        yield SimpleNamespace(create=create)

    monkeypatch.setattr(fixture.MailFixture, "connection", connection)
    return fixture.MailFixture(tmp_path), state


@pytest.mark.parametrize("error", [OSError("not listening"), "login"])
def test_mail_startup_budget_and_transient_readiness(mail_startup, error):
    mail, state = mail_startup
    if error != "login":
        state.error = error
    with mail as ready:
        assert ready is mail
        options = [a for a in state.argv if a.startswith("-Dgreenmail.startup.timeout=")]
        assert options == ["-Dgreenmail.startup.timeout=25000"]
        assert state.argv.index(options[0]) < state.argv.index("-jar")
        assert state.clock >= 3 and state.folders == ["Sent"]
    assert state.terminated and state.waited and mail.log.closed
    assert not mail.model_thread.is_alive()


def test_mail_startup_exception_preserves_origin_and_cleans_up(mail_startup):
    mail, state = mail_startup
    state.ready_at = float("inf")
    state.startup_log = (
        'Exception in thread "main" java.lang.IllegalStateException: '
        "Could not start mail server imaps:127.0.0.1:3993, "
        "try to set server startup timeout > 2000\n"
    )
    with (
        pytest.raises(RuntimeError, match="GreenMail startup failed:.*Could not start mail server"),
        mail,
    ):
        pytest.fail("half-started server accepted")
    assert state.clock == 0 and state.attempts <= 1
    assert state.terminated and state.waited and mail.log.closed
    assert not mail.model_thread.is_alive()


@pytest.mark.parametrize("exited", [False, True])
def test_mail_startup_readiness_deadline_and_process_exit(mail_startup, exited):
    mail, state = mail_startup
    state.ready_at = float("inf")
    state.startup_log = "public ordinary startup message\n"
    state.exit_code = 1 if exited else None
    with (
        pytest.raises(RuntimeError, match="GreenMail TLS/login did not become ready"),
        mail,
    ):
        pytest.fail("unready server accepted")
    if exited:
        assert state.clock == 0
    else:
        assert 30 <= state.clock < 30.2
        budget = [a for a in state.argv if a.startswith("-Dgreenmail.startup.timeout=")]
        assert len(budget) == 1
        assert int(budget[0].split("=", 1)[1]) < state.clock * 1000
    assert state.terminated and state.waited and mail.log.closed
    assert not mail.model_thread.is_alive()


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
$name = & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
    -Phase Name -InstallRoot $env:EW_ROOT
$same = & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
    -Phase Name -InstallRoot ($env:EW_ROOT.ToUpperInvariant())
$other = & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
    -Phase Name -InstallRoot ($env:EW_ROOT + '-other')
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
    $out = & powershell.exe -NoProfile -NonInteractive -EncodedCommand $env:EW_MUTEX_CHILD
    if ($LASTEXITCODE -ne 0 -or $out -ne 'BUSY') { throw 'Concurrent admission was not excluded' }
    'GLOBAL_KERNEL_EXCLUSION'
} finally { $m.ReleaseMutex(); $m.Dispose() }
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=powershell_environment(
            dict(
                os.environ, EW_SCRIPT=str(script), EW_ROOT=str(tmp_path / "installation"),
                EW_MUTEX_CHILD=encoded_powershell_script(
                    "$ErrorActionPreference='Stop'; "
                    "$m=[Threading.Mutex]::OpenExisting($env:EW_MUTEX); "
                    "try { if ($m.WaitOne(0)) { $m.ReleaseMutex(); "
                    "throw 'Concurrent admission' }; 'BUSY' } finally { $m.Dispose() }"
                ),
            )
        ),
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
        os.environ,
        EW_ROOT=str(root),
        EW_TASK=task,
        EW_SCRIPT=str(script),
        EW_CATALOG=str(catalog),
        EW_FIRST_DISABLE=str(tmp_path / "first-disable.json"),
        EW_ABORT_CHILD=encoded_powershell_script(
            "Import-Module ScheduledTasks; "
            "function Disable-ScheduledTask { param($TaskPath,$TaskName); "
            r"ScheduledTasks\Disable-ScheduledTask -TaskPath $TaskPath "
            "-TaskName $TaskName | Out-Null; "
            "[pscustomobject]@{Task=$TaskName;ProcessId=$PID} | ConvertTo-Json -Compress | "
            "Set-Content -LiteralPath $env:EW_FIRST_DISABLE -Encoding UTF8; "
            "exit 7 }; & $env:EW_SCRIPT -Phase Pre -InstallRoot $env:EW_ROOT "
            "-InstallerPid $env:EW_INSTALLER -ProgramCatalog $env:EW_CATALOG; exit $LASTEXITCODE"
        ),
    )

    def powershell(code, *, expected=0):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
            env=powershell_environment(environment),
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
            "$name= & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT "
            "-Phase Name -InstallRoot $env:EW_ROOT; "
            "$m=[Threading.Mutex]::new($false,$name); "
            "if (!$m.WaitOne(10000)) { throw 'Fixture installer busy' }; "
            "$env:EW_INSTALLER=$PID; "
            "& powershell.exe -NoProfile -NonInteractive "
            "-ExecutionPolicy RemoteSigned -EncodedCommand $env:EW_ABORT_CHILD; "
            "if ($LASTEXITCODE -ne 7) { throw 'Pre did not abort at the first mutation' }; "
            "if ((Get-ScheduledTask -TaskName $env:EW_TASK).State -ne 'Disabled') "
            "{ throw 'Pre did not disable task' }; exit 7",
            expected=7,
        )
        marker = json.loads((tmp_path / "first-disable.json").read_text(encoding="utf-8-sig"))
        assert marker["Task"] == task and marker["ProcessId"] > 0
        deadline = time.monotonic() + 30
        pending = root / "windows-cutover-pending-v1.json"
        while pending.exists():
            assert time.monotonic() < deadline, "Aborted installer did not restore task"
            time.sleep(0.05)
        assert powershell("(Get-ScheduledTask -TaskName $env:EW_TASK).State") == "Ready"
        assert not (root / windows_cutover.RECEIPT_NAME).exists()
    finally:
        powershell("Unregister-ScheduledTask -TaskName $env:EW_TASK -Confirm:$false")


def test_installed_removal_proof_keeps_real_pending_receipt_across_status_restart(
    tmp_path, monkeypatch,
):
    import prove_windows_cutover_full as proof
    from test_thread_bodies import body_runner
    from test_thread_reconcile import NOW, seed

    from eom_email_watcher import body_state, engine_api, source_locks
    from eom_email_watcher.db import Store
    from eom_email_watcher.runtime import Runtime

    watcher, _, _ = body_runner(tmp_path, monkeypatch, {})
    store = watcher.store
    seed(store, "other", rfc_message_id="other@fixture", thread_id="other-thread")
    body_state.record(store, "m", ("public held body", 16), fetched_at=NOW)
    with store.connection() as db:
        assert source_locks.protocol_ready(db)
    # Only the in-process twin shortens an existing wait. The installed proof
    # keeps the unmodified production deadline and actual subprocess transport.
    monkeypatch.setattr(source_locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    calls = []

    def removal_api(client, operation, payload=None):
        # A fresh actual Store for every call exercises initialization/status
        # without manufacturing a receipt or patching source qualification.
        reopened = Store(store.path)
        reopened.initialize()
        runtime = Runtime(watcher.config, reopened, watcher.model)
        with monkeypatch.context() as active:
            active.setattr(engine_api, "_runtime", lambda _: runtime)
            reply = engine_api._response({
                "protocol": 1, "operation": operation, "payload": payload or {},
            })
        assert reply["ok"], reply
        calls.append(operation)
        return reply["data"]

    monkeypatch.setattr(proof.Engine, "call", removal_api)
    client = proof.Engine(
        "unused-source-twin", tmp_path / "unused.toml", tmp_path, dict(os.environ),
    )
    resumed, key, pending, expected = proof.prove_removal_recovery(client, store.path)
    assert pending["outcome"] == "deferred", "Missing held-pending installed proof"
    assert (pending["requested"], pending["completed"], pending["pending"]) == (
        expected, 0, expected
    )
    assert expected == 2
    assert resumed is not client
    assert calls == ["inbox.removal.prepare", "inbox.clear", "inbox.removal.status"]
    assert proof.scalar(store.path, "SELECT COUNT(*) FROM messages") == 2
    assert proof.scalar(store.path, "SELECT COUNT(*) FROM logical_messages") == 0
    assert resumed.call("inbox.removal.status", {"request_id": key, "kind": "clear"}) == pending
    source_locks.qualify_protocol(Store(store.path), fresh=False)
    settled = resumed.call("inbox.removal.status", {"request_id": key, "kind": "clear"})
    assert (settled["requested"], settled["completed"], settled["pending"]) == (2, 2, 0)
    assert proof.scalar(store.path, "SELECT COUNT(*) FROM messages") == 0
