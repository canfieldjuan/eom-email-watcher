"""Integrated native catalog recovery proof; never imported by the product."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from prove_windows_cutover import (
    isolated_environment,
    run_installer,
    settle_case_cleanup,
    wait_file,
)
from windows_cutover_process_fixture import ScheduledActor
from windows_installer_template import catalog_macro
from windows_qualification_environment import encoded_powershell_script, powershell_environment

ROOT = Path(__file__).resolve().parents[1]
ABORT_CHILD = (
    "Import-Module ScheduledTasks; "
    "function Disable-ScheduledTask { param($TaskPath,$TaskName); "
    r"ScheduledTasks\Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName | Out-Null; "
    "[pscustomobject]@{Task=$TaskName;ProcessId=$PID} | ConvertTo-Json -Compress | "
    "Set-Content -LiteralPath $env:EW_FIRST_DISABLE -Encoding UTF8; exit 7 }; "
    "& $env:EW_SCRIPT -Phase Pre -InstallRoot $env:EW_ROOT "
    "-InstallerPid $env:EW_INSTALLER -ProgramCatalog $env:EW_CATALOG; exit $LASTEXITCODE"
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_template(desktop, engine, destination):
    """The catalog owner consumes the same File entries that ship the fixture."""

    def quote(value):
        return str(value).replace("$", "$$").replace('"', '$\\"')

    return (
        '!define MAINBINARYSRCPATH "' + quote(desktop) + '"\n'
        'Section Install\n  SetOutPath "$INSTDIR"\n'
        '  File "/oname=' + quote(destination) + '" "${MAINBINARYSRCPATH}"\n'
        "{{#each binaries}}\n"
        '  File "/oname=eom-mail-engine.exe" "' + quote(engine) + '"\n'
        "{{/each}}\n"
        '  CopyFiles /SILENT "$EW_PACKAGE_PROGRAM_CATALOG" '
        '"$INSTDIR\\windows-package-files-v1.txt"\nSectionEnd\n'
    )


def run_catalog_retry(output, *, retain_binding):
    assert os.name == "nt", "Native proof cannot be substituted on another platform"
    output.mkdir(parents=True, exist_ok=True)
    root = output / "installed-a"
    root.mkdir()
    stage = output / "package-b"
    private = output / "private"
    private.mkdir()
    task = "EW-Catalog-Retry-" + uuid.uuid4().hex
    script = ROOT / "desktop/src-tauri/windows/cutover.ps1"
    setup = list((ROOT / "desktop/src-tauri/target/release/bundle/nsis").glob("*-setup.exe"))
    assert len(setup) == 1, "Expected the actual built release package"
    # This is the same frozen, native predecessor actor as the primary lane.
    packet = Path(os.environ["EW_QUALIFICATION_TEST_OUTPUT"]).parent
    actor = packet / "build/dist/legacy-source-owner.exe"
    assert actor.is_file(), "The primary lane must have built its frozen actor"
    case = "catalog-retained-binding" if retain_binding else "catalog-absent-binding"
    state = {
        "result": "PENDING",
        "case": case,
        "account": os.environ["USERNAME"],
        "retained_binding": retain_binding,
        "task": task,
    }

    def save():
        (output / "catalog-retry.json").write_text(json.dumps(state, indent=2))

    save()  # Keep an unknown outcome even if native package setup fails.

    def command(label, code, *, expected=0):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
            env=powershell_environment(
                EW_SCRIPT=str(script),
                EW_ROOT=str(root),
                EW_TASK=task,
                EW_CATALOG=str(stage / "windows-package-files-v1.txt"),
                EW_FIRST_DISABLE=str(output / "first-disable.json"),
                EW_STATE=str(output / "retry-state.json"),
                EW_STAGE=str(stage),
                EW_ACTOR=str(retired),
                EW_ABORT_CHILD=encoded_powershell_script(ABORT_CHILD),
            ),
            capture_output=True,
            text=True,
            timeout=40,
        )
        (output / (label + ".stdout.log")).write_text(result.stdout)
        (output / (label + ".stderr.log")).write_text(result.stderr)
        assert result.returncode == expected, result.stdout + result.stderr
        return result.stdout

    # Native A installation creates its own authenticated catalog and receipt.
    install, _ = run_installer(setup[0], root, output, "install-a")
    assert install.returncode == 0, install.stdout + install.stderr
    installed_catalog = root / "windows-package-files-v1.txt"
    receipt = root / "windows-source-cutover-v1.json"
    rollback = root / "windows-cutover-rollback-v1.json"
    pending = root / "windows-cutover-pending-v1.json"
    old_catalog = installed_catalog.read_bytes()
    old_sha = sha(installed_catalog)
    initial = json.loads(receipt.read_text(encoding="utf-8-sig"))
    assert initial["program_catalog_sha256"] == old_sha
    assert initial["engine_sha256"] == sha(root / "eom-mail-engine.exe")
    lines = installed_catalog.read_text(encoding="utf-16").splitlines()
    identities = [
        line.removeprefix("EW-APP-IMAGE|") for line in lines if line.startswith("EW-APP-IMAGE|")
    ]
    desktop_names = [name for name in identities if name != "eom-mail-engine.exe"]
    assert len(desktop_names) == 1, "A main image must come from the actual package catalog"
    retired = root / desktop_names[0]
    assert desktop_names[0] in lines and retired.is_file()
    replacement = "renamed-" + retired.name

    # Compile B from A's real payload with one renamed image. The production
    # generator emits B's catalog from B's actual NSIS File instructions.
    compilers = (
        [Path(shutil.which("makensis"))]
        if shutil.which("makensis")
        else list(Path(os.environ["LOCALAPPDATA"]).rglob("makensis.exe"))
    )
    assert len(compilers) == 1, "Expected the existing native NSIS compiler"
    template = package_template(retired, root / "eom-mail-engine.exe", replacement)
    package = output / "package-b.exe"
    source = output / "package-b.nsi"
    generated = (
        'Unicode true\n!include "FileFunc.nsh"\n'
        'Name "EW catalog qualification only"\nRequestExecutionLevel user\n'
        f'OutFile "{package}"\nInstallDir "{stage}"\n'
        + catalog_macro(template)
        + "\nFunction .onInit\n  !insertmacro EW_PREPARE_PACKAGE_CATALOG\nFunctionEnd\n"
        + template
    )
    # This fixture has exactly one sidecar. Preserve its real binaries role
    # during catalog generation, then render that single-item loop for NSIS.
    generated = generated.replace("{{#each binaries}}", "").replace("{{/each}}", "")
    assert "{{" not in generated, "Fixture package has an unrendered catalog directive"
    source.write_text(generated, encoding="utf-8")
    for label, argv in (
        ("compile-b", [str(compilers[0]), str(source)]),
        ("extract-b", [str(package), "/S", "/D=" + str(stage)]),
    ):
        result = subprocess.run(argv, capture_output=True, timeout=40)
        (output / (label + ".stdout.log")).write_bytes(result.stdout)
        (output / (label + ".stderr.log")).write_bytes(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
    b_catalog = stage / "windows-package-files-v1.txt"
    b_lines = b_catalog.read_text(encoding="utf-16").splitlines()
    assert desktop_names[0] not in b_lines and replacement in b_lines
    assert "EW-APP-IMAGE|" + replacement in b_lines
    assert "EW-APP-IMAGE|eom-mail-engine.exe" in b_lines
    state.update(
        a_catalog_sha256=old_sha,
        b_catalog_sha256=sha(b_catalog),
        b_package_sha256=sha(package),
        b_generator_source_sha256=sha(source),
        b_generator_source=source.read_text(encoding="utf-8"),
        b_catalog=b_lines,
    )

    # Product ownership is catalog PATH membership, not program image hashes:
    # Read-PackagePrograms returns paths; Assert-OwnedImage uses set.Contains.
    # Assert the substitution is exactly an authenticated A package path. The
    # independent populated-operation lane still uses the installed real engine.
    shutil.copy2(actor, retired)
    assert retired.relative_to(root).as_posix() in lines
    assert sha(retired) == sha(actor)
    state["actor_substitution"] = {
        "ownership_rule": "authenticated catalog path membership",
        "catalog_path": desktop_names[0],
        "actual_path": str(retired),
        "actor_sha256": sha(actor),
    }
    save()
    registered = False
    process = None
    held = None
    log = None
    try:
        registered = True  # Include partially registered tasks in cleanup.
        command(
            "register-a",
            r"""
$ErrorActionPreference='Stop'
$a=New-ScheduledTaskAction -Execute $env:EW_ACTOR
$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(1)
Register-ScheduledTask -TaskName $env:EW_TASK -Action $a -Trigger $t | Out-Null
""",
        )
        command(
            "abort-b",
            r"""
$ErrorActionPreference='Stop'
$name=& powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
    -Phase Name -InstallRoot $env:EW_ROOT
$m=[Threading.Mutex]::new($false,$name)
if (!$m.WaitOne(10000)) { throw 'Fixture installer busy' }
$env:EW_INSTALLER=$PID
& powershell.exe -NoProfile -NonInteractive -EncodedCommand $env:EW_ABORT_CHILD
if ($LASTEXITCODE -ne 7) { throw 'Pre did not abort at first mutation' }
exit 7
""",
            expected=7,
        )
        marker = json.loads((output / "first-disable.json").read_text(encoding="utf-8-sig"))
        assert marker["Task"] == task and marker["ProcessId"] > 0
        deadline = time.monotonic() + 30  # Existing first-disable recovery oracle.
        while pending.exists():
            assert time.monotonic() < deadline, "Aborted Pre did not restore A task"
            time.sleep(0.05)
        assert (
            command("restored-a", "(Get-ScheduledTask -TaskName $env:EW_TASK).State").strip()
            == "Ready"
        )
        assert not receipt.exists()
        binding = json.loads(rollback.read_text(encoding="utf-8-sig"))
        assert binding["version"] == 1 and binding["catalog_sha256"] == old_sha
        assert base64.b64decode(binding["catalog"]) == old_catalog
        state["aborted_pre"] = {
            "first_disable": marker,
            "task_state": "Ready",
            "receipt_absent": True,
            "authenticated_rollback": binding,
        }
        if not retain_binding:
            rollback.unlink()  # Negative fixture input, never product state repair.
        save()
        ready = output / "restarted-a.ready"
        log = (output / "restarted-a.log").open("w")
        process = subprocess.Popen(
            [
                str(retired),
                str(private / "old.sqlite3"),
                str(ready),
                str(output / "unused-trigger"),
                "http://127.0.0.1:9/public",
                "idle",
            ],
            env=isolated_environment(private),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        wait_file(ready, process)
        actor_pid = int(ready.with_suffix(".pid").read_text())
        held = ScheduledActor(actor_pid)
        state["restarted_actor"] = {"launcher_pid": process.pid, "actor_pid": actor_pid}
        save()
        result = command(
            "retry-b",
            r"""
$ErrorActionPreference='Stop'
$name=& powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
    -Phase Name -InstallRoot $env:EW_ROOT
$m=[Threading.Mutex]::new($false,$name)
if (!$m.WaitOne(10000)) { throw 'Fixture installer busy' }
try {
    & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
        -Phase Pre -InstallRoot $env:EW_ROOT -InstallerPid $PID `
        -ProgramCatalog $env:EW_CATALOG
    $pre=$LASTEXITCODE
    if ($pre -ne 0) { exit $pre }
    $scheduler=New-Object -ComObject Schedule.Service; $scheduler.Connect()
    $before=[ordered]@{
        task_enabled=$scheduler.GetFolder('\').GetTask($env:EW_TASK).Enabled
        receipt_present=(Test-Path (Join-Path $env:EW_ROOT 'windows-source-cutover-v1.json'))
        rollback_present=(Test-Path (Join-Path $env:EW_ROOT 'windows-cutover-rollback-v1.json'))
        running=@(Get-CimInstance Win32_Process | Where-Object {
            $_.ExecutablePath -eq $env:EW_ACTOR }).Count
    }
    $before | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:EW_STATE -Encoding UTF8
    if ($before.task_enabled -or $before.receipt_present -or
        !$before.rollback_present -or $before.running) {
        throw 'Retry Pre did not quiesce authenticated A owners'
    }
    $replacement=Join-Path $env:EW_STAGE ('renamed-'+[IO.Path]::GetFileName($env:EW_ACTOR))
    Copy-Item -LiteralPath (Join-Path $env:EW_STAGE 'eom-mail-engine.exe'),$replacement `
        -Destination $env:EW_ROOT -Force
    & powershell.exe -NoProfile -NonInteractive -File $env:EW_SCRIPT `
        -Phase Post -InstallRoot $env:EW_ROOT -InstallerPid $PID `
        -ProgramCatalog $env:EW_CATALOG
    exit $LASTEXITCODE
} finally { $m.ReleaseMutex(); $m.Dispose() }
""",
            expected=0 if retain_binding else 1,
        )
        if retain_binding:
            assert f"Cutover stopping process={actor_pid}" in result
            assert "Cutover disabled task=\\" + task in result
            process.wait(timeout=10)
            pre = json.loads((output / "retry-state.json").read_text(encoding="utf-8-sig"))
            assert pre == {
                "task_enabled": False,
                "receipt_present": False,
                "rollback_present": True,
                "running": 0,
            }
            current = json.loads(receipt.read_text(encoding="utf-8-sig"))
            assert current["program_catalog_sha256"] == sha(b_catalog) == sha(installed_catalog)
            assert current["engine_sha256"] == sha(root / "eom-mail-engine.exe")
            assert not rollback.exists() and not pending.exists()
            assert (
                command("final-task", "(Get-ScheduledTask -TaskName $env:EW_TASK).State").strip()
                == "Ready"
            )
            state["completion"] = {"pre": pre, "receipt": current, "rollback_retired": True}
        else:
            assert "Unknown installation image: " + str(retired) in result
            assert process.poll() is None
            assert held.kernel.WaitForSingleObject(held.handle, 0) == 258
            assert not receipt.exists() and not rollback.exists() and not pending.exists()
            assert (
                command("refused-task", "(Get-ScheduledTask -TaskName $env:EW_TASK).State").strip()
                == "Ready"
            )
            state["completion"] = {
                "refusal": "unknown installation image",
                "actor_alive": True,
                "task_state": "Ready",
                "receipt_absent": True,
            }
        state["result"] = "OPERATIONS_PASSED"
        save()
    finally:
        primary = sys.exception()
        operations = []
        if registered:
            for action in ("Stop", "Unregister"):
                code = "$ErrorActionPreference='Stop'; " + action
                code += "-ScheduledTask -TaskName $env:EW_TASK"
                if action == "Unregister":
                    code += " -Confirm:$false"
                operations.append(
                    (
                        action + " A task",
                        lambda script=code, label=action: command("cleanup-" + label, script),
                    )
                )
        if held is not None:
            operations.append(("held A actor", held.settle))
        if process is not None:

            def stop_launcher():
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)

            operations.append(("A launcher", stop_launcher))
        if log is not None:
            operations.append(("actor log", log.close))
        cleanup = settle_case_cleanup(
            output / "case-cleanup.json",
            os.environ["USERNAME"],
            case,
            operations,
            primary=primary,
        )
        state["cleanup"] = cleanup
        state["result"] = "PASS" if primary is None and not cleanup["errors"] else "FAIL"
        save()
        assert not cleanup["errors"], cleanup["errors"]
    return state
