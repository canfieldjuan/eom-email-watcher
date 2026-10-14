"""Local owners plus native regressions for the accepted continuation."""

import os
from pathlib import Path

import pytest
from windows_qualification_environment import powershell_environment

ROOT = Path(__file__).resolve().parents[1]


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
$script:items=@([pscustomobject]@{Name='new-app.exe';ExecutablePath=$null;ProcessId=98762})
$refused=$false
try { Installed-Processes } catch { $refused=$true }
if (!$refused) { throw 'Inaccessible application identity ignored' }
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
