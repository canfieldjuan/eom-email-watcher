param(
    [Parameter(Mandatory=$true)][ValidateSet('Name','Pre','Post','Restore')][string]$Phase,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [int]$InstallerPid = 0,
    [long]$InstallerStarted = 0,
    [string]$ProgramCatalog = ''
)
$ErrorActionPreference = 'Stop'
$stage = 'initialization'
$operation = 'phase-entry'
$targetProcess = 0
function Write-CutoverFailure($record, [string]$context) {
    $failure = $record.Exception
    $base = $failure.GetBaseException()
    $code = if ($base -is [ComponentModel.Win32Exception]) { $base.NativeErrorCode } else { '' }
    Write-Output ("Cutover failure-stage=$stage process=$targetProcess code=$code context=$context operation=$operation")
    while ($failure) {
        Write-Output ("Cutover exception type=" + $failure.GetType().FullName +
            " HResult=" + $failure.HResult + " message=" + $failure.Message)
        if ($failure -is [ComponentModel.Win32Exception]) {
            Write-Output ("Cutover native-error=" + $failure.NativeErrorCode)
        }
        $failure = $failure.InnerException
    }
    Write-Output ("Cutover error-id=" + $record.FullyQualifiedErrorId +
        " position=" + $record.InvocationInfo.PositionMessage)
}
function Open-CutoverProcessHandle($process) {
    # A property access suppresses getter exceptions in PowerShell. The method
    # preserves Win32Exception and the same Process object's cached handle.
    return $process.get_Handle()
}
trap { Write-CutoverFailure $_ 'phase'; exit 1 }
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
$installedCatalog = Join-Path $root 'windows-package-files-v1.txt'
$rollback = Join-Path $root 'windows-cutover-rollback-v1.json'
function File-Sha256([string]$path, [byte[]]$content = $null) {
    $hash = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = if ($null -ne $content) {
            [IO.MemoryStream]::new($content, $false)
        } else { [IO.File]::OpenRead($path) }
        return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $hash.Dispose()
    }
}
function Resolve-ExecutablePath([string]$path, [string]$context = 'path') {
    try {
        if ([string]::IsNullOrWhiteSpace($path)) { return $null }
        $value = [Environment]::ExpandEnvironmentVariables($path.Trim())
        if ($value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2).Trim()
        }
        if (![IO.Path]::IsPathRooted($value) -or $value -match '%[^%]+%') {
            throw 'Unresolved executable path'
        }
        return [IO.Path]::GetFullPath($value)
    } catch { Write-Warning ("Ignoring unresolvable $context path '$path': " + $_.Exception.Message); return $null }
}
function Is-InstalledPath([string]$path) {
    return $path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}
function Read-PackagePrograms([string]$catalog, [bool]$identityOnly = $false, [byte[]]$content = $null) {
    if ($null -eq $content -and (!(Test-Path -LiteralPath $catalog -PathType Leaf) -or
        ((Get-Item -LiteralPath $catalog -Force).Attributes -band [IO.FileAttributes]::ReparsePoint))) {
        throw 'Package program catalog is missing or unsafe'
    }
    if ($null -eq $content) { $content = [IO.File]::ReadAllBytes($catalog) }
    $lines = [Text.Encoding]::Unicode.GetString($content).TrimStart([char]0xfeff) -split '\r?\n'
    if (!$lines.Length -or $lines[0] -ne 'EW-PACKAGE-FILES-V1') { throw 'Invalid package program catalog' }
    foreach ($entry in $lines | Select-Object -Skip 1) {
        if ([string]::IsNullOrWhiteSpace($entry)) { continue }
        $identity = $entry.StartsWith('EW-APP-IMAGE|', [StringComparison]::Ordinal)
        if ($identity) { $entry = $entry.Substring('EW-APP-IMAGE|'.Length) }
        if ($identityOnly -ne $identity) { continue }
        if ([IO.Path]::IsPathRooted($entry)) { throw 'Package path must be relative' }
        $full = [IO.Path]::GetFullPath((Join-Path $root $entry))
        if (!(Is-InstalledPath $full)) { throw 'Package path escapes installation' }
        if ([IO.Path]::GetExtension($full) -ieq '.exe') { $full }
    }
}
function Prior-Catalog {
    if (Test-Path -LiteralPath $rollback) {
        $record = Get-Content -LiteralPath $rollback -Raw | ConvertFrom-Json
        $bytes = [Convert]::FromBase64String($record.catalog)
        if ($record.version -ne 1 -or (File-Sha256 '' $bytes) -ne $record.catalog_sha256) {
            throw 'Prior package rollback binding is invalid'
        }
        return ,$bytes
    }
    if ((Test-Path -LiteralPath $installedCatalog) -and (Test-Path -LiteralPath $receipt)) {
        $previous = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        if ($previous.program_catalog_sha256 -and
            (File-Sha256 $installedCatalog) -ieq $previous.program_catalog_sha256) {
            return ,([IO.File]::ReadAllBytes($installedCatalog))
        }
    }
    return $null
}
function Save-PriorCatalog {
    $bytes = Prior-Catalog
    if ($null -eq $bytes -or (Test-Path -LiteralPath $rollback)) { return }
    $record = [ordered]@{version=1;catalog=[Convert]::ToBase64String($bytes);catalog_sha256=(File-Sha256 '' $bytes)}
    $temp = $rollback + '.tmp'
    ConvertTo-Json -InputObject $record -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $rollback -Force
}
function Load-OwnedPrograms {
    $set = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $script:identityNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($path in @(Read-PackagePrograms $ProgramCatalog)) { $set.Add($path) | Out-Null }
    foreach ($path in @(Read-PackagePrograms $ProgramCatalog $true)) {
        $script:identityNames.Add([IO.Path]::GetFileName($path)) | Out-Null
    }
    if (!$set.Count) { throw 'Package has no executable programs' }
    $prior = Prior-Catalog
    if ($null -ne $prior) {
        foreach ($path in @(Read-PackagePrograms '' $false $prior)) { $set.Add($path) | Out-Null }
        foreach ($path in @(Read-PackagePrograms '' $true $prior)) {
            $script:identityNames.Add([IO.Path]::GetFileName($path)) | Out-Null
        }
    }
    return ,$set
}
function Assert-OwnedImage([string]$path) {
    if (!(Is-InstalledPath $path) -or !$ownedPrograms.Contains($path)) {
        throw ("Unknown installation image: " + $path)
    }
}
function Task-Enabled($task) {
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    return [bool]$scheduler.GetFolder($task.TaskPath).GetTask($task.TaskName).Enabled
}
function Start-RestorationOwner {
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
function Restore-Tasks {
    if (Test-Path -LiteralPath $pending) {
        $failures = @()
        $savedTasks = Get-Content -LiteralPath $pending -Raw | ConvertFrom-Json
        foreach ($saved in $savedTasks) {
            try {
                if ($saved.Enabled) { Enable-ScheduledTask -TaskPath $saved.TaskPath -TaskName $saved.TaskName | Out-Null }
            } catch {
                $failures += ($saved.TaskPath + $saved.TaskName + ': ' + $_.Exception.Message)
            }
        }
        if ($failures.Count) { throw ('Task restoration failed: ' + ($failures -join '; ')) }
        Remove-Item -LiteralPath $pending
    }
}
if ($Phase -eq 'Restore') {
    # Keep the original process object/handle, not a PID that can be reused.
    $installer = Get-Process -Id $InstallerPid -ErrorAction SilentlyContinue
    if ($installer) {
        $handle = Open-CutoverProcessHandle $installer
        if ($installer.StartTime.ToUniversalTime().Ticks -eq $InstallerStarted) {
            Set-Content -LiteralPath $restoreReady -Value 'ready' -Encoding ASCII
            $installer.WaitForExit()
        }
    }
    # Serialize restoration with every installer transaction. An older worker
    # cannot re-enable a task while the next update is stopping its owners.
    $stage = 'restoration-exclusion'
    $operation = 'mutex-create'
    $mutex = [Threading.Mutex]::new($false, (Installer-MutexName))
    $owned = $false
    $restoreFailure = $null
    try {
        Write-Output 'Cutover stage=restoration-exclusion'
        $operation = 'mutex-wait'
        try { $owned = $mutex.WaitOne(90000) }
        catch [Threading.AbandonedMutexException] { $owned = $true }
        if (!$owned) { throw 'Task restoration deferred behind an active installer' }
        $operation = 'task-restoration'
        Restore-Tasks
        Remove-Item -LiteralPath $restoreReady,$restoreScript -ErrorAction SilentlyContinue
    } catch {
        $restoreFailure = $_
        Write-CutoverFailure $_ 'restoration'
        throw
    } finally {
        try {
            $operation = 'mutex-release'
            if ($owned) { $mutex.ReleaseMutex() }
            $operation = 'mutex-dispose'
            $mutex.Dispose()
        } catch {
            Write-CutoverFailure $_ 'restoration-cleanup'
            if (!$restoreFailure) { throw }
        }
    }
    exit 0
}
# Add-Type's compiler must never resolve System.dll from an invoker's plugin
# directory. PowerShell location and the process-wide .NET directory are distinct.
Set-Location -LiteralPath ([Environment]::SystemDirectory)
[Environment]::CurrentDirectory = [Environment]::SystemDirectory
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class CutoverProcess {
    [StructLayout(LayoutKind.Sequential)]
    struct Luid { public uint Low; public int High; }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")]
    static extern uint GetCurrentProcessId();
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, int length, out int needed);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool LookupPrivilegeValue(string system, string name, out Luid luid);
    [DllImport("advapi32.dll")]
    static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")]
    static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
    static IntPtr TokenBuffer(IntPtr token, int kind) {
        int needed;
        GetTokenInformation(token, kind, IntPtr.Zero, 0, out needed);
        if (needed <= 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        var buffer = Marshal.AllocHGlobal(needed);
        if (!GetTokenInformation(token, kind, buffer, needed, out needed)) {
            int error = Marshal.GetLastWin32Error();
            Marshal.FreeHGlobal(buffer);
            throw new Win32Exception(error);
        }
        return buffer;
    }
    // Query-only: no token mutation or policy input. No user SID is emitted.
    public static string Token(uint pid) {
        bool own = pid == GetCurrentProcessId();
        IntPtr process = own ? GetCurrentProcess() : OpenProcess(0x1000, false, pid);
        if (process == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        IntPtr token = IntPtr.Zero, elevation = IntPtr.Zero, integrity = IntPtr.Zero,
            privileges = IntPtr.Zero;
        try {
            if (!OpenProcessToken(process, 0x0008, out token))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            elevation = TokenBuffer(token, 20);
            integrity = TokenBuffer(token, 25);
            privileges = TokenBuffer(token, 3);
            bool elevated = Marshal.ReadInt32(elevation) != 0;
            IntPtr sid = Marshal.ReadIntPtr(integrity);
            byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            int rid = Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(count - 1)));
            Luid debug;
            if (!LookupPrivilegeValue(null, "SeDebugPrivilege", out debug))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            bool present = false, enabled = false;
            int entries = Marshal.ReadInt32(privileges);
            for (int i = 0; i < entries; ++i) {
                int offset = 4 + i * 12;
                if ((uint)Marshal.ReadInt32(privileges, offset) == debug.Low &&
                    Marshal.ReadInt32(privileges, offset + 4) == debug.High) {
                    present = true;
                    enabled = (Marshal.ReadInt32(privileges, offset + 8) & 2) != 0;
                }
            }
            return String.Format(System.Globalization.CultureInfo.InvariantCulture,
                "{{\"pid\":{0},\"elevated\":{1},\"integrity_rid\":{2},\"debug_present\":{3},\"debug_enabled\":{4}}}",
                pid, elevated.ToString().ToLowerInvariant(), rid,
                present.ToString().ToLowerInvariant(), enabled.ToString().ToLowerInvariant());
        } finally {
            if (privileges != IntPtr.Zero) Marshal.FreeHGlobal(privileges);
            if (integrity != IntPtr.Zero) Marshal.FreeHGlobal(integrity);
            if (elevation != IntPtr.Zero) Marshal.FreeHGlobal(elevation);
            if (token != IntPtr.Zero) CloseHandle(token);
            if (!own) CloseHandle(process);
        }
    }
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
function Write-CutoverToken([int]$processId, [string]$role) {
    try { Write-Output ("Cutover token role=$role data=" + [CutoverProcess]::Token($processId)) }
    catch { Write-Warning ("Token diagnostic unavailable role=$role process=" + $processId + ": " + $_.Exception.Message) }
}
Write-CutoverToken $PID 'stop-host-entry'
if ($InstallerPid -gt 0) { Write-CutoverToken $InstallerPid 'installer' }
function Installed-Processes {
    foreach ($process in Get-CimInstance Win32_Process) {
        if ($process.ProcessId -eq $InstallerPid) { continue }
        $path = Resolve-ExecutablePath $process.ExecutablePath ('process ' + $process.ProcessId)
        if (Is-InstalledPath $path) { Assert-OwnedImage $path; $process }
        elseif (!$process.ExecutablePath -and $identityNames.Contains($process.Name)) {
            throw 'Cannot establish the identity of a running Email Watcher process'
        }
    }
}
if ($Phase -eq 'Pre') {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    foreach ($slot in @($root,$receipt,$pending,$restoreScript,$restoreReady,$installedCatalog,$rollback,($rollback + '.tmp'),(Join-Path $root 'windows-source-cutover-v1.tmp'))) {
        if (Test-Path -LiteralPath $slot) {
            $item = Get-Item -LiteralPath $slot -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installer state cannot be a reparse point' }
        }
    }
    $ownedPrograms = Load-OwnedPrograms
    Restore-Tasks
    Save-PriorCatalog
    if (Test-Path -LiteralPath $receipt) { Remove-Item -LiteralPath $receipt }
    $saved = @()
    $stage = 'scheduled-task-selection'
    $targetProcess = 0
    try {
        Write-Output 'Cutover stage=scheduled-task-selection'
        $selected = @()
        foreach ($task in Get-ScheduledTask) {
            $matched = $false
            foreach ($action in $task.Actions) {
                $path = Resolve-ExecutablePath $action.Execute ('task ' + $task.TaskPath + $task.TaskName)
                if (Is-InstalledPath $path) {
                    Assert-OwnedImage $path
                    $matched = $true
                }
            }
            if ($matched) {
                $selected += $task
                $saved += [pscustomobject]@{TaskName=$task.TaskName;TaskPath=$task.TaskPath;Enabled=(Task-Enabled $task)}
            }
        }
        if ($selected.Count) {
            ConvertTo-Json -InputObject $saved -Compress | Set-Content -LiteralPath $pending -Encoding UTF8
            Start-RestorationOwner
            foreach ($task in $selected) {
                Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
                Stop-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName
                Write-Output ('Cutover disabled task=' + $task.TaskPath + $task.TaskName)
            }
        }
        $stage = 'process-stop'
        $operation = 'process-enumeration'
        foreach ($process in @(Installed-Processes)) {
            Write-Output ('Cutover stopping process=' + $process.ProcessId)
            Write-CutoverToken $PID 'stop-host-before-get-process'
            Write-CutoverToken $process.ProcessId 'actor-before-stop'
            $targetProcess = $process.ProcessId
            $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($live) {
                # Open the process handle and validate its live image before Kill;
                # a recycled PID must never authorize killing an unrelated process.
                $processFailure = $null
                try {
                    $operation = 'process-handle'
                    Write-CutoverToken $PID 'stop-host-before-handle'
                    $handle = Open-CutoverProcessHandle $live
                    Write-CutoverToken $PID 'stop-host-after-handle'
                    $operation = 'held-image'
                    $image = Resolve-ExecutablePath ([CutoverProcess]::Image($handle)) 'held process'
                    if (!(Is-InstalledPath $image)) { continue }
                    Assert-OwnedImage $image
                    $operation = 'process-termination'
                    Write-CutoverToken $PID 'stop-host-before-terminate'
                    [CutoverProcess]::Stop($handle)
                } catch {
                    $processFailure = $_
                    Write-CutoverFailure $_ 'process'
                } finally {
                    try {
                        $operation = 'process-dispose'
                        $live.Dispose()
                    } catch {
                        Write-CutoverFailure $_ 'process-cleanup'
                        if (!$processFailure) { throw }
                    }
                }
                if ($processFailure) { throw $processFailure }
            }
        }
        if (@(Installed-Processes).Count) { throw 'An old installation is still running' }
    } catch {
        $originalFailure = $_
        Write-CutoverFailure $originalFailure 'pre'
        try {
            Restore-Tasks
            Write-Output 'Cutover stage=tasks-restored-after-failure'
        } catch { Write-CutoverFailure $_ 'rollback' }
        throw $originalFailure
    }
    exit 0
}
# NSIS executes Post only after every replacement has succeeded. Before then,
# disabled tasks and stopped desktop producers cannot restart the old image.
$stage = 'publication'
$operation = 'process-enumeration'
$ownedPrograms = Load-OwnedPrograms
if (@(Installed-Processes).Count) { throw 'Publication cannot qualify while an installation owner is running' }
$engine = Join-Path $root 'eom-mail-engine.exe'
if (!(Test-Path -LiteralPath $engine -PathType Leaf)) { throw 'The installed engine is missing' }
Copy-Item -LiteralPath $ProgramCatalog -Destination $installedCatalog -Force
$operation = 'receipt-hash'
$record = [ordered]@{version=1;protocol='source-stripe-v1';engine_sha256=(File-Sha256 $engine);program_catalog_sha256=(File-Sha256 $installedCatalog)}
$temp = Join-Path $root 'windows-source-cutover-v1.tmp'
ConvertTo-Json -InputObject $record -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
Move-Item -LiteralPath $temp -Destination $receipt -Force
Restore-Tasks
if (Test-Path -LiteralPath $rollback) { Remove-Item -LiteralPath $rollback }
exit 0
