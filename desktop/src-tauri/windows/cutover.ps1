param(
    [Parameter(Mandatory=$true)][ValidateSet('Name','Pre','Post','Restore')][string]$Phase,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [int]$InstallerPid = 0,
    [long]$InstallerStarted = 0
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'Installation root cannot be a drive root' }
$prefix = $root + '\'
function Installer-MutexName {
    # Kernel Global namespace spans sessions; the canonical root separates installs.
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant())
        $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
        return 'Global\EomEmailWatcherSourceCutoverV1-' + $digest
    } finally { $hash.Dispose() }
}
if ($Phase -eq 'Name') { [Console]::Write((Installer-MutexName)); exit 0 }
# Diagnostics do not participate in admission. NSIS also records each invocation's
# exit/output, including syntax or Name failures that cannot start a transcript.
try {
    $trace = Join-Path ([IO.Path]::GetTempPath()) ('cutover-' + $InstallerPid + '-' + $Phase + '-' + $PID + '.txt')
    Start-Transcript -Path $trace -Force | Out-Null
} catch { Write-Warning ('Cutover transcript unavailable: ' + $_.Exception.Message) }
Write-Output ('Cutover phase=' + $Phase + ' root=' + $root + ' process=' + $PID)
$receipt = Join-Path $root 'windows-source-cutover-v1.json'
$pending = Join-Path $root 'windows-cutover-pending-v1.json'
$restoreScript = Join-Path $root 'windows-cutover-restore-v1.ps1'
$restoreReady = Join-Path $root 'windows-cutover-restore-ready-v1'
function Is-InstalledPath([string]$path) {
    if (!$path) { return $false }
    return [IO.Path]::GetFullPath($path).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}
function Restore-Tasks {
    if (Test-Path -LiteralPath $pending) {
        foreach ($saved in @(Get-Content -LiteralPath $pending -Raw | ConvertFrom-Json)) {
            if ($saved.Enabled) { Enable-ScheduledTask -TaskPath $saved.TaskPath -TaskName $saved.TaskName | Out-Null }
        }
        Remove-Item -LiteralPath $pending
    }
}
if ($Phase -eq 'Restore') {
    # Keep the original process object/handle, not a PID that can be reused.
    $installer = Get-Process -Id $InstallerPid -ErrorAction SilentlyContinue
    if ($installer -and $installer.StartTime.ToUniversalTime().Ticks -eq $InstallerStarted) {
        $handle = $installer.Handle
        Set-Content -LiteralPath $restoreReady -Value 'ready' -Encoding ASCII
        $installer.WaitForExit()
    }
    # Serialize restoration with every installer transaction. An older worker
    # cannot re-enable a task while the next update is stopping its owners.
    $mutex = [Threading.Mutex]::new($false, (Installer-MutexName))
    $owned = $false
    try {
        Write-Output 'Cutover stage=restoration-exclusion'
        try { $owned = $mutex.WaitOne(90000) }
        catch [Threading.AbandonedMutexException] { $owned = $true }
        if (!$owned) { throw 'Task restoration deferred behind an active installer' }
        Restore-Tasks
        Remove-Item -LiteralPath $restoreReady,$restoreScript -ErrorAction SilentlyContinue
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    exit 0
}
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class CutoverProcess {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool QueryFullProcessImageName(IntPtr h, int flags, StringBuilder path, ref int size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateProcess(IntPtr h, uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint WaitForSingleObject(IntPtr h, uint timeout);
    public static string Image(IntPtr h) {
        var path = new StringBuilder(32768); int size = path.Capacity;
        if (!QueryFullProcessImageName(h, 0, path, ref size))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return path.ToString();
    }
    public static void Stop(IntPtr h) {
        if (!TerminateProcess(h, 1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        if (WaitForSingleObject(h, 10000) != 0) throw new TimeoutException("An old engine did not exit");
    }
}
'@
function Installed-Processes {
    foreach ($process in Get-CimInstance Win32_Process) {
        if ($process.ProcessId -eq $InstallerPid) { continue }
        if (Is-InstalledPath $process.ExecutablePath) { $process }
        elseif (!$process.ExecutablePath -and $process.Name -in @('eom-mail-engine.exe','eom-email-watcher-desktop.exe')) {
            throw 'Cannot establish the identity of a running Email Watcher process'
        }
    }
}
if ($Phase -eq 'Pre') {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    foreach ($slot in @($root,$receipt,$pending,$restoreScript,$restoreReady,(Join-Path $root 'windows-source-cutover-v1.tmp'))) {
        if (Test-Path -LiteralPath $slot) {
            $item = Get-Item -LiteralPath $slot -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installer state cannot be a reparse point' }
        }
    }
    Restore-Tasks
    if (Test-Path -LiteralPath $receipt) { Remove-Item -LiteralPath $receipt }
    $saved = @()
    try {
        Write-Output 'Cutover stage=scheduled-task-selection'
        foreach ($task in Get-ScheduledTask) {
            if (@($task.Actions | Where-Object { Is-InstalledPath $_.Execute }).Count) {
                foreach ($action in @($task.Actions | Where-Object { Is-InstalledPath $_.Execute })) {
                    if ([IO.Path]::GetFileName($action.Execute) -notin @('eom-mail-engine.exe','eom-email-watcher-desktop.exe') -or
                        [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($action.Execute)) -ne $root) {
                        throw 'A scheduled task points to an unsupported old installation image'
                    }
                }
                $saved += [pscustomobject]@{TaskName=$task.TaskName;TaskPath=$task.TaskPath;Enabled=($task.State -ne 'Disabled')}
                # Persist each addition before changing the task, so rollback is durable.
                ConvertTo-Json -InputObject $saved -Compress | Set-Content -LiteralPath $pending -Encoding UTF8
                Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
                Stop-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName
                Write-Output ('Cutover disabled task=' + $task.TaskPath + $task.TaskName)
            }
        }
        if ($saved.Count) {
            if ($InstallerPid -le 0) { throw 'An installer handle is required to restore scheduled tasks' }
            $installer = Get-Process -Id $InstallerPid
            $started = $installer.StartTime.ToUniversalTime().Ticks
            Copy-Item -LiteralPath $PSCommandPath -Destination $restoreScript -Force
            if (Test-Path -LiteralPath $restoreReady) { Remove-Item -LiteralPath $restoreReady }
            $arguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' + $restoreScript + '" -Phase Restore -InstallRoot "' + $root + '" -InstallerPid ' + $InstallerPid + ' -InstallerStarted ' + $started
            $worker = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
            $deadline = [DateTime]::UtcNow.AddSeconds(10)
            while (!(Test-Path -LiteralPath $restoreReady)) {
                if ($worker.HasExited -or [DateTime]::UtcNow -ge $deadline) { throw 'Task-restoration owner did not become ready' }
                Start-Sleep -Milliseconds 50
            }
        }
        foreach ($process in @(Installed-Processes)) {
            Write-Output ('Cutover stopping process=' + $process.ProcessId)
            $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($live) {
                # Open the process handle and validate its live image before Kill;
                # a recycled PID must never authorize killing an unrelated process.
                try {
                    $handle = $live.Handle
                    if (!(Is-InstalledPath ([CutoverProcess]::Image($handle)))) { continue }
                    [CutoverProcess]::Stop($handle)
                } finally { $live.Dispose() }
            }
        }
        if (@(Installed-Processes).Count) { throw 'An old installation is still running' }
    } catch {
        Write-Output ('Cutover failure=' + $_.Exception.Message)
        Restore-Tasks
        Write-Output 'Cutover stage=tasks-restored-after-failure'
        throw
    }
    exit 0
}
# NSIS executes Post only after every replacement has succeeded. Before then,
# disabled tasks and stopped desktop producers cannot restart the old image.
if (@(Installed-Processes).Count) { throw 'Publication cannot qualify while an installation owner is running' }
$engine = Join-Path $root 'eom-mail-engine.exe'
if (!(Test-Path -LiteralPath $engine -PathType Leaf)) { throw 'The installed engine is missing' }
$record = [ordered]@{version=1;protocol='source-stripe-v1';engine_sha256=(Get-FileHash -LiteralPath $engine -Algorithm SHA256).Hash.ToLowerInvariant()}
$temp = Join-Path $root 'windows-source-cutover-v1.tmp'
ConvertTo-Json -InputObject $record -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
Move-Item -LiteralPath $temp -Destination $receipt -Force
Restore-Tasks
