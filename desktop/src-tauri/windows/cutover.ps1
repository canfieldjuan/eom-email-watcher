param(
    [Parameter(Mandatory=$true)][ValidateSet('Pre','Post','Restore')][string]$Phase,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [int]$InstallerPid = 0,
    [long]$InstallerStarted = 0
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'Installation root cannot be a drive root' }
$prefix = $root + '\'
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
    $mutex = [Threading.Mutex]::new($false, 'Local\EomEmailWatcherSourceCutoverV1')
    $owned = $false
    try {
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
            $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($live) {
                # Open the process handle and validate its live image before Kill;
                # a recycled PID must never authorize killing an unrelated process.
                $handle = $live.Handle
                if (!(Is-InstalledPath $live.Path)) { continue }
                $live.Kill()
                if (!$live.WaitForExit(10000)) { throw 'An old engine did not exit' }
            }
        }
        if (@(Installed-Processes).Count) { throw 'An old installation is still running' }
    } catch { Restore-Tasks; throw }
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
