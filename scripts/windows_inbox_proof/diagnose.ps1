param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Evidence
)
$ErrorActionPreference = 'Stop'

$installation = Join-Path $Evidence 'diagnostic-app'
$installed = Start-Process -FilePath $Installer -ArgumentList ('/S /D=' + $installation) -Wait -PassThru
if ($installed.ExitCode -ne 0) { throw "Diagnostic installer exited $($installed.ExitCode)" }
$desktop = Join-Path $installation 'eom-email-watcher-desktop.exe'
$engine = Join-Path $installation 'eom-mail-engine.exe'
$engineHash = (Get-FileHash -LiteralPath $engine).Hash
if ($engineHash -ne 'aedcd11df8ded8a220c5d55373ee64d1c60a9d522b806e158c8be239034412d3') {
    throw 'Diagnostic install changed the shipped engine'
}

$env:EOM_EMAIL_WATCHER_CONFIG = Join-Path $Evidence 'fixture/config.toml'
$env:EOM_EMAIL_WATCHER_PROOF_ADMISSION_TRACE = '1'
$env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $Evidence 'diagnostic-webview'
Remove-Item Env:EOM_EMAIL_ENGINE_BIN -ErrorAction SilentlyContinue
$stderr = Join-Path $Evidence 'desktop-stderr.raw'
$stdout = Join-Path $Evidence 'desktop-stdout.raw'
$etl = Join-Path $Evidence 'windows-processes.etl'
$profile = Join-Path $PSScriptRoot 'trace/EngineLifecycle.wprp'
& wpr.exe -start "$profile!EngineLifecycle.Light"
if ($LASTEXITCODE -ne 0) { throw "Could not start Windows process and image trace: $LASTEXITCODE" }
try {
    $app = Start-Process -FilePath $desktop -WorkingDirectory $installation -RedirectStandardError $stderr -RedirectStandardOutput $stdout -PassThru
    # Return evidence to the hosted job if the GUI stalls without a decision.
    # This collection window does not change the app's request deadlines.
    $until = (Get-Date).AddSeconds(55)
    do {
        Start-Sleep -Milliseconds 250
        $app.Refresh()
        $trace = @(Get-Content -LiteralPath $stderr | Where-Object {
            $_.StartsWith('admission proof: ') -or $_.StartsWith('configuration admission failed at ')
        })
        $decision = @($trace | Where-Object { $_.StartsWith('configuration admission failed at ') })
        $comparison = @($trace | Where-Object { $_.StartsWith('admission proof: config.admission.compare output_collected ') })
    } until ($app.HasExited -or $decision.Count -or $comparison.Count -gt 1 -or (Get-Date) -ge $until)
} finally {
    & wpr.exe -stop $etl
    if ($LASTEXITCODE -ne 0) { throw "Could not save Windows process trace: $LASTEXITCODE" }
}

$trace | Set-Content -LiteralPath (Join-Path $Evidence 'desktop-admission-trace.txt') -Encoding utf8NoBOM
[ordered]@{
    installer_sha256=(Get-FileHash -LiteralPath $Installer).Hash
    desktop_sha256=(Get-FileHash -LiteralPath $desktop).Hash
    engine_sha256=$engineHash
    process_id=$app.Id
    desktop_exited=$app.HasExited
    decision=$decision
    trace_records=$trace.Count
} | ConvertTo-Json -Depth 4 | Tee-Object -FilePath (Join-Path $Evidence 'diagnostic.json')
$trace
if (-not $trace.Count) { throw 'The installed diagnostic desktop emitted no admission trace' }
$parser = Join-Path $PSScriptRoot 'trace/EngineTrace.csproj'
dotnet run --project $parser --configuration Release --no-build -- $etl $app.Id (Join-Path $Evidence 'engine-delay.json')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
