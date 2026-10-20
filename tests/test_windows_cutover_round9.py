"""Local owners plus native regressions for the accepted continuation."""

import os
from pathlib import Path

import pytest
from windows_qualification_environment import powershell_environment

ROOT = Path(__file__).resolve().parents[1]


def test_compiler_identity_replays_actual_tauri_verbose_build_line():
    from windows_catalog_retry_fixture import compiler_from_build_log

    # Literal formatter output from run38605083419: module field and the
    # doubled space emitted by pinned CommandExt's argument fold.
    compiler = r"C:\Users\runneradmin\AppData\Local\tauri\NSIS\makensis.exe"
    line = (
        "\x1b[31;1m     Running [tauri_bundler::utils] Command `" + compiler
        + "  -INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 -V3 "
        + r"D:\a\eom-email-watcher\eom-email-watcher\desktop\src-tauri\target\release"
        + r"\nsis\x64\installer.nsi`" + "\x1b[0m"
    )
    assert str(compiler_from_build_log(line)) == compiler


@pytest.mark.parametrize("module", ["", " [tauri_bundler::utils]"])
@pytest.mark.parametrize("separator", [" ", "  ", "\t"])
def test_compiler_identity_preserves_formatter_whitespace_and_optional_module(
    tmp_path, module, separator
):
    from windows_catalog_retry_fixture import compiler_from_build_log

    compiler = tmp_path / "makensis.exe"
    line = (
        f"Running{module} Command `{compiler}{separator}"
        "-INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 -V3 installer.nsi`"
    )
    assert compiler_from_build_log(line) == compiler


def test_compiler_identity_comes_from_invoked_build_command_not_recursive_inventory(tmp_path):
    import windows_catalog_retry_fixture as fixture

    compiler = tmp_path / "tools with spaces/NSIS/makensis.exe"
    other = tmp_path / "tools with spaces/NSIS/Bin/makensis.exe"
    compiler.parent.mkdir(parents=True)
    other.parent.mkdir()
    compiler.write_bytes(b"public invoked compiler")
    other.write_bytes(b"public unused compiler")
    log = f"Running Command `{compiler} -INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 -V4 installer.nsi`"
    owner = getattr(fixture, "compiler_from_build_log", None)
    assert callable(owner), "The fixture has no invoked-compiler identity owner"
    assert owner(log) == compiler
    assert owner(log.replace("Running", "\x1b[32mRunning\x1b[0m")) == compiler


@pytest.mark.parametrize(
    "log", [
        "", "Running makensis to produce setup.exe", "Command `makensis.exe -V4 installer.nsi`",
        "Running Command `relative/makensis.exe -INPUTCHARSET UTF8 "
        "-OUTPUTCHARSET UTF8 -V4 installer.nsi`",
    ]
)
def test_compiler_identity_refuses_missing_command_evidence(log):
    from windows_catalog_retry_fixture import compiler_from_build_log

    with pytest.raises(ValueError, match="compiler command"):
        compiler_from_build_log(log)


def test_compiler_identity_refuses_multiple_build_commands(tmp_path):
    from windows_catalog_retry_fixture import compiler_from_build_log

    one = (
        f"Running Command `{tmp_path}/makensis.exe "
        "-INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 -V4 installer.nsi`"
    )
    with pytest.raises(ValueError, match="compiler command"):
        compiler_from_build_log(one + "\n" + one)


@pytest.mark.parametrize("changed", ["compiler", "log", "receipt"])
def test_compiler_receipt_is_bound_to_actual_executable_and_build(tmp_path, changed):
    import windows_catalog_retry_fixture as fixture

    folder = tmp_path / "full"
    folder.mkdir()
    compiler = tmp_path / "makensis.exe"
    compiler.write_bytes(b"public compiler bytes")
    log = folder / "nsis-build.log"
    log.write_text(
        f"Running Command `{compiler} -INPUTCHARSET UTF8 -OUTPUTCHARSET UTF8 -V4 installer.nsi`"
    )
    receipt = folder / "nsis-compiler.json"
    fixture.record_build_compiler(log, receipt)
    assert fixture.qualified_compiler(tmp_path) == compiler
    if changed == "compiler":
        compiler.write_bytes(b"replaced compiler")
    elif changed == "log":
        log.write_text(log.read_text() + "\nreplacement build log")
    else:
        receipt.write_text(receipt.read_text().replace("compiler_sha256", "other_hash"))
    with pytest.raises((ValueError, KeyError)):
        fixture.qualified_compiler(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native installed catalog abort/retry")
@pytest.mark.parametrize("retain_binding", [True, False])
def test_native_installed_catalog_abort_then_retry(tmp_path, retain_binding):
    from windows_catalog_retry_fixture import run_catalog_retry

    result = run_catalog_retry(tmp_path, retain_binding=retain_binding)
    assert result["result"] == "PASS"
    assert result["cleanup"]["quiesced"] is True


def test_file_hash_owner_has_no_cmdlet_autoload_dependency():
    source = (ROOT / "desktop/src-tauri/windows/cutover.ps1").read_text()
    assert "Get-FileHash" not in source, "File hashing still depends on module autoload"
    assert "function File-Sha256" in source


def test_package_roles_are_derived_in_the_inventory_macro():
    from windows_installer_template import catalog_macro

    template = """Section Install
  File "${MAINBINARYSRCPATH}"
  {{#each binaries}}
  File /a "/oname={{this}}" "{{no-escape @key}}"
  {{/each}}
  {{#each resources}}
  File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
  {{/each}}
  WriteUninstaller "$INSTDIR\\uninstall.exe"
SectionEnd
"""
    macro = catalog_macro(template)
    assert "EW-APP-IMAGE|" in macro, "Package membership lacks application identity roles"
    assert "EW-APP-IMAGE|{{this}}" in macro
    assert "EW-APP-IMAGE|uninstall.exe" not in macro
    assert "EW-APP-IMAGE|{{this.[1]}}" not in macro


@pytest.mark.parametrize(
    "pre,post,valid",
    [
        ("0", "0", True),
        ("1", None, True),
        ("1", "0", False),
        (None, "0", False),
        ("timeout", "0", False),
    ],
)
def test_phase_outcome_oracle_rejects_post_after_failed_pre(tmp_path, pre, post, valid):
    from prove_windows_cutover import phase_outcomes

    for phase, code in (("Pre", pre), ("Post", post)):
        if code is not None:
            (tmp_path / f"eom-email-watcher-cutover-42-{phase}.log").write_text(
                f"phase={phase} exit={code} output=public fixture\n"
            )
    if valid:
        assert phase_outcomes(tmp_path).get("Pre") == ([pre] if pre else None)
    else:
        with pytest.raises(AssertionError, match="Post ran"):
            phase_outcomes(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native catalog retry, file hashing and CIM boundary")
def test_native_catalog_retry_and_missing_path_identity(tmp_path):
    import hashlib
    import subprocess

    root = tmp_path / "installation"
    root.mkdir()
    old = root / "windows-package-files-v1.txt"
    old.write_text(
        "EW-PACKAGE-FILES-V1\nretired-app.exe\nEW-APP-IMAGE|retired-app.exe\n",
        encoding="utf-16",
    )
    old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
    (root / "windows-source-cutover-v1.json").write_text(
        '{"program_catalog_sha256":"' + old_sha + '"}', encoding="utf-8"
    )
    catalog = tmp_path / "new-package.txt"
    catalog.write_text(
        "EW-PACKAGE-FILES-V1\nnew-app.exe\nEW-APP-IMAGE|new-app.exe\n"
        "uninstall.exe\nnew-resource-helper.exe\n",
        encoding="utf-16",
    )
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$limit=$source.IndexOf('if ($Phase -eq ''Pre'')')
. ([scriptblock]::Create($source.Substring(0,$limit))) -Phase Pre `
    -InstallRoot $env:EW_ROOT -ProgramCatalog $env:EW_CATALOG
function Get-FileHash { throw 'Cmdlet autoload dependency' }
$ownedPrograms=Load-OwnedPrograms
if ((File-Sha256 $installedCatalog) -ne $env:EW_OLD_SHA) { throw 'Wrong digest' }
$retired=Join-Path $root 'retired-app.exe'
if (!$ownedPrograms.Contains($retired)) { throw 'Prior program missing' }
Save-PriorCatalog
Remove-Item -LiteralPath $receipt
[IO.File]::WriteAllText($installedCatalog,'public incomplete replacement')
$ownedPrograms=Load-OwnedPrograms
if (!$ownedPrograms.Contains($retired)) { throw 'Retry lost authenticated prior bytes' }
function Get-CimInstance { param($ClassName); $script:items }
$script:items=@([pscustomobject]@{Name='uninstall.exe';ExecutablePath=$null;ProcessId=98761})
if (@(Installed-Processes).Count) { throw 'Generic inaccessible image attributed' }
$script:items=@([pscustomobject]@{
    Name='new-resource-helper.exe';ExecutablePath=(Join-Path $root 'new-resource-helper.exe');
    ProcessId=98763
})
if (@(Installed-Processes).Count -ne 1) { throw 'Shipped full-path helper rejected' }
'CATALOG_RETRY_IDENTITY_PASS'
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=powershell_environment(
            dict(
                os.environ,
                EW_SCRIPT=str(ROOT / "desktop/src-tauri/windows/cutover.ps1"),
                EW_ROOT=str(root),
                EW_CATALOG=str(catalog),
                EW_OLD_SHA=old_sha,
            )
        ),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CATALOG_RETRY_IDENTITY_PASS" in result.stdout.splitlines()


@pytest.mark.skipif(os.name != "nt", reason="native path-less CIM process race")
def test_native_pathless_identity_rechecks_process_liveness(tmp_path):
    import subprocess

    root = tmp_path / "installation"
    root.mkdir()
    catalog = tmp_path / "package.txt"
    catalog.write_text(
        "EW-PACKAGE-FILES-V1\nowned.exe\nEW-APP-IMAGE|owned.exe\n", encoding="utf-16"
    )
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$limit=$source.IndexOf('if ($Phase -eq ''Pre'')')
. ([scriptblock]::Create($source.Substring(0,$limit))) -Phase Pre `
    -InstallRoot $env:EW_ROOT -ProgramCatalog $env:EW_CATALOG
$ownedPrograms=Load-OwnedPrograms
function Get-CimInstance { param($ClassName); $script:items }
function Get-Process {
    param($Id, $ErrorAction)
    if ($script:lookup -eq 'real') {
        return Microsoft.PowerShell.Management\Get-Process -Id $Id -ErrorAction Stop
    }
    if ($script:lookup -eq 'blocked') { return $script:blocked }
}
$script:items=@([pscustomobject]@{Name='owned.exe';ExecutablePath=$null;ProcessId=98761})
$script:lookup='gone'
if (@(Installed-Processes).Count) { throw 'Exited process blocked cutover' }
$script:blocked=[pscustomobject]@{HasExited=$false}
$script:blocked | Add-Member ScriptMethod get_Handle { throw 'public inaccessible handle' }
$script:blocked | Add-Member ScriptMethod Dispose { }
$script:items=@([pscustomobject]@{Name='owned.exe';ExecutablePath=$null;ProcessId=98762})
$script:lookup='blocked'
$script:blocked.HasExited=$true
if (@(Installed-Processes).Count) { throw 'Process exit during handle lookup blocked cutover' }
$script:blocked.HasExited=$false
$stage='process-stop'
$refusal=$null
try { Installed-Processes | Out-Null } catch { $refusal=$_ }
if (!$refusal -or $refusal.Exception.Message -notmatch '98762.*owned\.exe') {
    throw 'Live unreadable identity did not refuse with PID and name'
}
$diagnostic=@(Write-CutoverFailure $refusal 'process') -join "`n"
if ($diagnostic -notmatch 'failure-stage=process-stop process=98762 .*image=owned\.exe') {
    throw 'Cutover failure line omitted PID or image name'
}
$real=Microsoft.PowerShell.Management\Get-Process -Id $PID
try {
    $image=[CutoverProcess]::Image((Open-CutoverProcessHandle $real))
    $root=[IO.Path]::GetDirectoryName($image)
    $prefix=$root.TrimEnd('\')+'\'
    $ownedPrograms=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $ownedPrograms.Add($image) | Out-Null
    $identityNames=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $identityNames.Add([IO.Path]::GetFileName($image)) | Out-Null
    $script:items=@(
        [pscustomobject]@{
            Name=[IO.Path]::GetFileName($image);ExecutablePath=$null;ProcessId=2147483647
        },
        [pscustomobject]@{
            Name=[IO.Path]::GetFileName($image);ExecutablePath=$null;ProcessId=$PID
        }
    )
    $script:lookup='real'
    if (@(Installed-Processes).Count -ne 1) { throw 'Mixed process identities misclassified' }
    $ownedPrograms.Clear()
    $refusal=$null
    try { Installed-Processes | Out-Null } catch { $refusal=$_ }
    if (!$refusal -or $refusal.Exception.Message -notmatch 'Unknown installation image: ') {
        throw 'Recovered unknown image bypassed package ownership'
    }
} finally { $real.Dispose() }
'PATHLESS_IDENTITY_RECHECK_PASS'
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=powershell_environment(
            dict(
                os.environ,
                EW_SCRIPT=str(ROOT / "desktop/src-tauri/windows/cutover.ps1"),
                EW_ROOT=str(root),
                EW_CATALOG=str(catalog),
            )
        ),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PATHLESS_IDENTITY_RECHECK_PASS" in result.stdout.splitlines()


@pytest.mark.skipif(os.name != "nt", reason="native unhandled phase failure exit/diagnostics")
def test_native_phase_failure_exits_nonzero_with_exception_chain(tmp_path):
    import subprocess

    catalog = tmp_path / "package.txt"
    catalog.write_text("EW-PACKAGE-FILES-V1\npublic.exe\n", encoding="utf-16")
    root = tmp_path / "installation"
    root.mkdir()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "desktop/src-tauri/windows/cutover.ps1"),
            "-Phase",
            "Post",
            "-InstallRoot",
            str(root),
            "-ProgramCatalog",
            str(catalog),
        ],
        env=powershell_environment(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode != 0
    assert "The installed engine is missing" in result.stdout
    assert "Cutover exception type=" in result.stdout and "HResult=" in result.stdout
