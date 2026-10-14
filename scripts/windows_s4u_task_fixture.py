"""Case-bound elevated S4U registration, never product scheduler policy."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from windows_batch_logon import account_rights

S4U = 2
LIMITED = 0
MAX_REQUEST_BYTES = 16384
PRINCIPAL_ID = r"""function Principal-Sid([string]$value) {
    if ($value.StartsWith('S-')) {
        return [Security.Principal.SecurityIdentifier]::new($value).Value
    }
    return [Security.Principal.NTAccount]::new($value).Translate(
        [Security.Principal.SecurityIdentifier]).Value
}
"""


def validate_request(request, output, account, task):
    """A standard child cannot choose the privileged registration's target."""
    if not isinstance(request, dict) or set(request) != {"account", "task", "engine", "argv"}:
        raise ValueError("Malformed S4U fixture request")
    if request["account"] != account or request["task"] != task:
        raise ValueError("S4U request belongs to another case")
    folder = Path(output) / "old-then-new"
    engine = folder / "installed/eom-mail-engine.exe"
    argv = request["argv"]
    expected = [
        str(folder / "private/mail.sqlite3"),
        str(folder / "scheduled.ready"),
        str(folder / "resume-old"),
    ]
    if request["engine"] != str(engine) or not isinstance(argv, list) or len(argv) != 5:
        raise ValueError("S4U request selected another actor")
    if any(type(arg) is not str for arg in argv) or argv[:3] != expected or argv[4] != "idle":
        raise ValueError("S4U request changed case arguments")
    endpoint = urlsplit(argv[3])
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.username is not None
        or endpoint.password is not None
        or not endpoint.port
        or endpoint.path != "/public-job"
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("S4U request selected another protocol peer")
    return engine, argv


REGISTER = (
    PRINCIPAL_ID
    + r"""$ErrorActionPreference='Stop'
$scheduler=New-Object -ComObject Schedule.Service
$scheduler.Connect()
$definition=$scheduler.NewTask(0)
$definition.RegistrationInfo.Author=$env:EW_S4U_SID
$definition.Principal.UserId=$env:EW_S4U_SID
$definition.Principal.LogonType=[int]$env:EW_S4U_LOGON
$definition.Principal.RunLevel=[int]$env:EW_S4U_LEVEL
$definition.Settings.Enabled=$true
$trigger=$definition.Triggers.Create(1)
$trigger.StartBoundary=(Get-Date).AddDays(1).ToString('s')
$action=$definition.Actions.Create(0)
$action.Path=$env:EW_S4U_ENGINE
$action.Arguments=$env:EW_S4U_ARGS
$sid=$env:EW_S4U_SID
$evidence=[ordered]@{principal=$sid;logon_type=[int]$env:EW_S4U_LOGON
    run_level=[int]$env:EW_S4U_LEVEL;flags=2
    target_rights=@($env:EW_S4U_RIGHTS | ConvertFrom-Json)
    token=$null;registering_sid=$null;attempts=@();selected=$null}
try {
    # Reuse the existing query-only token owner in this exact registration host.
    $source=Get-Content -LiteralPath $env:EW_S4U_SOURCE -Raw
    $prefix=$source.Substring(0,$source.IndexOf('function Installed-Processes'))
    . ([scriptblock]::Create($prefix)) -Phase Pre -InstallRoot $env:EW_S4U_OUTPUT | Out-Null
    $evidence.token=[CutoverProcess]::Token($PID) | ConvertFrom-Json
    $evidence.registering_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (!$evidence.token.elevated -or $evidence.token.integrity_rid -lt 12288) {
        throw 'S4U registration requires the elevated outer fixture token'
    }
    if ($evidence.target_rights -notcontains 'SeBatchLogonRight') {
        throw 'S4U target batch right is absent at registration'
    }
    $dacl='D:P(A;;GA;;;'+$sid+')(A;;GA;;;SY)(A;;GA;;;BA)'
    $task=$null
    foreach ($variant in @('explicit-owner','dacl-only')) {
        $sddl=if ($variant -eq 'explicit-owner') {'O:'+$sid+$dacl} else {$dacl}
        $attempt=[ordered]@{variant=$variant;sddl=$sddl;status='pending';hresult=$null;error=$null}
        try {
            $task=$scheduler.GetFolder('\').RegisterTaskDefinition(
                $env:EW_S4U_TASK,$definition,2,$sid,$null,[int]$env:EW_S4U_LOGON,$sddl)
            $attempt.status='registered';$attempt.hresult=0
            $evidence.selected=$variant
        } catch {
            $attempt.status='failed';$attempt.hresult=$_.Exception.GetBaseException().HResult
            $attempt.error=$_.Exception.ToString()
        }
        $evidence.attempts+=@($attempt)
        $evidence | ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $env:EW_S4U_DIAGNOSTICS -Encoding UTF8
        if ($null -ne $task) { break }
    }
    if ($null -eq $task) { throw 'Both S4U registration variants failed; see original HRESULTs' }
} finally {
    $evidence | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $env:EW_S4U_DIAGNOSTICS -Encoding UTF8
}
[ordered]@{task=$task.Name;principal=(Principal-Sid $task.Definition.Principal.UserId)
    logon_type=[int]$task.Definition.Principal.LogonType
    run_level=[int]$task.Definition.Principal.RunLevel
    sddl=$task.GetSecurityDescriptor(7);enabled=$task.Enabled
    engine=$task.Definition.Actions.Item(1).Path
    arguments=$task.Definition.Actions.Item(1).Arguments} | ConvertTo-Json -Compress
"""
)


class S4URegistration:
    """Own the one request and settle its native operation before resource cleanup."""

    def __init__(self, output, account, sid, task, shell):
        self.output, self.account, self.sid, self.task, self.shell = (
            output,
            account,
            sid,
            task,
            shell,
        )
        self.request = output / "standard-user-task-request.json"
        self.response = output / "standard-user-task-registration.json"
        self.stop = threading.Event()
        self.thread = None
        self.settled = False
        self.outcome = {"account": account, "task": task, "status": "not-requested"}

    def start(self):
        assert not self.request.exists() and not self.response.exists(), "Stale task proof"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.wait(0.05):
            if self.request.exists():
                break
        else:
            return
        try:
            with self.request.open("rb") as stream:
                content = stream.read(MAX_REQUEST_BYTES + 1)
            if len(content) > MAX_REQUEST_BYTES:
                raise ValueError("Oversized S4U fixture request")
            engine, argv = validate_request(
                json.loads(content), self.output, self.account, self.task
            )
            rights = account_rights(self.sid)
            self.outcome["target_rights"] = rights
            native = json.loads(
                self.shell(
                    REGISTER,
                    EW_S4U_SID=self.sid,
                    EW_S4U_TASK=self.task,
                    EW_S4U_ENGINE=str(engine),
                    EW_S4U_ARGS=subprocess.list2cmdline(argv),
                    EW_S4U_LOGON=str(S4U),
                    EW_S4U_LEVEL=str(LIMITED),
                    EW_S4U_RIGHTS=json.dumps(rights),
                    EW_S4U_SOURCE=str(
                        Path(__file__).resolve().parents[1]
                        / "desktop/src-tauri/windows/cutover.ps1"
                    ),
                    EW_S4U_OUTPUT=str(self.output),
                    EW_S4U_DIAGNOSTICS=str(self.output / "standard-user-task-diagnostics.json"),
                )
            )
            if (
                native["task"] != self.task
                or native["principal"] != self.sid
                or native["logon_type"] != S4U
                or native["run_level"] != LIMITED
                or native["engine"] != str(engine)
                or native["enabled"] is not True
                or native["arguments"] != subprocess.list2cmdline(argv)
            ):
                raise ValueError("Registered S4U task differs from the requested case")
            self.outcome.update(status="registered", native=native)
        except Exception as error:
            self.outcome.update(status="failed", error=str(error))
        finally:
            temporary = self.response.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.outcome, indent=2))
            temporary.replace(self.response)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(40)  # Same bounded native PowerShell operation budget.
            if self.thread.is_alive():
                raise RuntimeError("S4U registration remains in flight; retain fixture resources")
        self.settled = True


def request_s4u_task(output, task, engine, argv, endpoint_shell):
    """Standard-token control proof precedes every installer refusal/retry probe."""
    account = os.environ["USERNAME"]
    registration = output / "standard-user-task-registration.json"
    request = output / "standard-user-task-request.json"
    temporary = request.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"account": account, "task": task, "engine": str(engine), "argv": argv})
    )
    temporary.replace(request)
    deadline = time.monotonic() + 40
    while not registration.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Elevated S4U registration did not complete")
        time.sleep(0.02)
    result = json.loads(registration.read_text())
    assert result["account"] == account and result["task"] == task
    assert result["status"] == "registered", result
    control = endpoint_shell(
        PRINCIPAL_ID
        + r"""$ErrorActionPreference='Stop'
$scheduler=New-Object -ComObject Schedule.Service; $scheduler.Connect()
$task=$scheduler.GetFolder('\').GetTask($env:EW_TASK)
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ((Principal-Sid $task.Definition.Principal.UserId) -ne $sid -or
    [int]$task.Definition.Principal.LogonType -ne [int]$env:EW_S4U_LOGON -or
    [int]$task.Definition.Principal.RunLevel -ne [int]$env:EW_S4U_LEVEL) {
    throw 'S4U principal differs from standard token'
}
$results=[ordered]@{get=$false;disable=$false;enable=$false;stop=$false}
$errors=@()
foreach ($operation in @('get','disable','enable','stop')) {
    try {
        switch ($operation) {
            'get' { Get-ScheduledTask -TaskName $env:EW_TASK | Out-Null }
            'disable' {
                Disable-ScheduledTask -TaskName $env:EW_TASK | Out-Null
                if ($scheduler.GetFolder('\').GetTask($env:EW_TASK).Enabled) {
                    throw 'Task remained enabled'
                }
            }
            'enable' {
                Enable-ScheduledTask -TaskName $env:EW_TASK | Out-Null
                if (!$scheduler.GetFolder('\').GetTask($env:EW_TASK).Enabled) {
                    throw 'Task remained disabled'
                }
            }
            'stop' {
                Stop-ScheduledTask -TaskName $env:EW_TASK
                if ($scheduler.GetFolder('\').GetTask($env:EW_TASK).GetInstances(0).Count) {
                    throw 'Task instances remain active'
                }
            }
        }
        $results[$operation]=$true
    } catch { $errors+=($_.Exception.Message) }
}
[ordered]@{account=$env:USERNAME;task=$task.Name;sid=$sid;operations=$results;errors=$errors} |
    ConvertTo-Json -Depth 4 -Compress
""",
        EW_TASK=task,
        EW_S4U_LOGON=str(S4U),
        EW_S4U_LEVEL=str(LIMITED),
    )
    control = json.loads(control)
    (output / "standard-user-task-control.json").write_text(json.dumps(control, indent=2))
    assert control["account"] == account and control["task"] == task
    assert control["sid"] == result["native"]["principal"]
    assert not control["errors"] and control["operations"] == {
        "get": True,
        "disable": True,
        "enable": True,
        "stop": True,
    }, control
    return control
