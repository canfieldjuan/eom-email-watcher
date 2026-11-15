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

    $env:EOM_EMAIL_WATCHER_CONFIG = Join-Path $Fixture 'config.toml'
    $env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $Evidence 'webview'
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=9222'
    Remove-Item Env:EOM_EMAIL_ENGINE_BIN -ErrorAction SilentlyContinue
    $app = Start-Process -FilePath $desktop -WorkingDirectory $installation -PassThru
    "Installed desktop PID $($app.Id) in session $($app.SessionId)"
    & (Join-Path $PSScriptRoot 'measure-webview.ps1') -Output (Join-Path $Evidence 'inbox.json')
} finally {
    Stop-Transcript | Out-Null
}
