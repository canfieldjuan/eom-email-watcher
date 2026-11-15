param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Fixture,
    [Parameter(Mandatory=$true)][string]$Evidence
)
$ErrorActionPreference = 'Stop'

function Assert-Hash($Path, $Expected) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $Expected) { throw "Mismatched SHA-256 for $Path : $actual" }
    "SHA-256 $actual  $Path"
}

Start-Transcript -Path (Join-Path $Evidence 'launch.txt') | Out-Null
try {
    "Runner session: $([Diagnostics.Process]::GetCurrentProcess().SessionId)"
    "Interactive desktop reported: $([Environment]::UserInteractive)"
    Assert-Hash $Installer '62f916e639eae293506edabf816d966ea013231483ab9d912961daf0f99da86a'

    $installation = Join-Path $Evidence 'app'
    $attempt = Join-Path $Evidence 'installer'
    New-Item -ItemType Directory -Path $attempt | Out-Null
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    try {
        $env:TEMP = $env:TMP = $attempt
        $run = Start-Process -FilePath $Installer -ArgumentList ('/S /D=' + $installation) -Wait -PassThru
        "Installer exit code: $($run.ExitCode)"
        if ($run.ExitCode -ne 0) { throw "Installer exited $($run.ExitCode)" }
    } finally {
        $env:TEMP = $previousTemp
        $env:TMP = $previousTmp
    }

    $desktop = Join-Path $installation 'eom-email-watcher-desktop.exe'
    $engine = Join-Path $installation 'eom-mail-engine.exe'
    Assert-Hash $desktop '12090df2582faa592033783734fdaf5794246a99ab010a39d1eca488e9a6b0b8'
    Assert-Hash $engine 'aedcd11df8ded8a220c5d55373ee64d1c60a9d522b806e158c8be239034412d3'

    foreach ($name in 'ordinary-write-config.toml','config.toml') {
        $configPath = Join-Path $Fixture $name
        $request = @{protocol=1; operation='config.admission.snapshot'; config_path=$configPath; payload=@{}}
        $reply = ($request | ConvertTo-Json -Compress) | & $engine | Out-String | ConvertFrom-Json
        $status = @{name=$name; ok=$reply.ok; error=$reply.error}
        $status | ConvertTo-Json -Compress | Tee-Object -FilePath (Join-Path $Evidence 'admission.jsonl') -Append
        if ($name -eq 'config.toml' -and -not $reply.ok) { throw 'Packaged engine refused the private fixture config' }
    }

    $env:EOM_EMAIL_WATCHER_CONFIG = Join-Path $Fixture 'config.toml'
    $env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $Evidence 'webview'
    $browserArguments = 'HKCU:\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments'
    New-Item -Path $browserArguments -Force | Out-Null
    New-ItemProperty -Path $browserArguments -Name 'eom-email-watcher-desktop.exe' -PropertyType String -Value '--remote-debugging-port=9222' | Out-Null
    Remove-Item Env:EOM_EMAIL_ENGINE_BIN -ErrorAction SilentlyContinue
    $app = Start-Process -FilePath $desktop -WorkingDirectory $installation -PassThru
    "Installed desktop PID $($app.Id) in session $($app.SessionId)"
    try {
        & (Join-Path $PSScriptRoot 'measure-webview.ps1') -Output (Join-Path $Evidence 'inbox.json')
    } catch {
        $app.Refresh()
        "Desktop exited: $($app.HasExited)"
        if ($app.HasExited) {
            "Desktop exit code: $($app.ExitCode)"
        } else {
            "Desktop window: $($app.MainWindowTitle) ($($app.MainWindowHandle))"
        }
        Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe' OR Name = 'eom-email-watcher-desktop.exe' OR Name = 'eom-mail-engine.exe'" |
            Select-Object Name, ProcessId, ParentProcessId, SessionId, CommandLine |
            ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $Evidence 'processes.json')
        Get-NetTCPConnection -LocalPort 9222 -ErrorAction SilentlyContinue |
            Select-Object LocalAddress, State, OwningProcess |
            ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $Evidence 'listener.json')
        try {
            Add-Type -AssemblyName System.Windows.Forms,System.Drawing
            $bounds = [Windows.Forms.Screen]::PrimaryScreen.Bounds
            $bitmap = [Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
            try {
                $graphics = [Drawing.Graphics]::FromImage($bitmap)
                try {
                    $graphics.CopyFromScreen($bounds.Location, [Drawing.Point]::Empty, $bounds.Size)
                    $bitmap.Save((Join-Path $Evidence 'desktop.png'), [Drawing.Imaging.ImageFormat]::Png)
                } finally { $graphics.Dispose() }
            } finally { $bitmap.Dispose() }
        } catch {
            "Could not capture the hosted test desktop: $_"
        }
        throw
    }
} finally {
    Stop-Transcript | Out-Null
}
