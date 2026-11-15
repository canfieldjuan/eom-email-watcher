param([Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference = 'Stop'
$target = $null
$until = (Get-Date).AddSeconds(35)
while (-not $target -and (Get-Date) -lt $until) {
    try {
        $targets = Invoke-RestMethod -Uri 'http://127.0.0.1:9222/json/list' -TimeoutSec 2
        $target = $targets | Where-Object {
            $url = $null
            $_.type -eq 'page' -and [uri]::TryCreate($_.url, [UriKind]::Absolute, [ref]$url) -and
                ($url.Host -eq 'tauri.localhost' -or $url.Scheme -eq 'tauri')
        } | Select-Object -First 1
    } catch {
        Start-Sleep -Milliseconds 250
    }
    if (-not $target) { Start-Sleep -Milliseconds 250 }
}
if (-not $target.webSocketDebuggerUrl) { throw 'Could not identify the packaged WebView2 target' }

$socket = [Net.WebSockets.ClientWebSocket]::new()
$deadline = [Threading.CancellationTokenSource]::new([TimeSpan]::FromSeconds(120))
try {
    $token = $deadline.Token
    $null = $socket.ConnectAsync([uri]$target.webSocketDebuggerUrl, $token).GetAwaiter().GetResult()
    $script = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'measure-inbox.js') -Raw
    $command = @{id=1; method='Runtime.evaluate'; params=@{expression=$script; awaitPromise=$true; returnByValue=$true}}
    $encoded = [Text.Encoding]::UTF8.GetBytes(($command | ConvertTo-Json -Depth 10 -Compress))
    $null = $socket.SendAsync([ArraySegment[byte]]::new($encoded), [Net.WebSockets.WebSocketMessageType]::Text, $true, $token).GetAwaiter().GetResult()
    $buffer = [byte[]]::new(16384)
    do {
        $message = [IO.MemoryStream]::new()
        try {
            do {
                $read = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $token).GetAwaiter().GetResult()
                if ($read.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw 'WebView2 closed' }
                $message.Write($buffer, 0, $read.Count)
            } until ($read.EndOfMessage)
            $response = [Text.Encoding]::UTF8.GetString($message.ToArray()) | ConvertFrom-Json
        } finally {
            $message.Dispose()
        }
    } until ($response.id -eq 1)
    $response | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Output -Encoding UTF8
    if ($response.error -or $response.result.exceptionDetails -or -not $response.result.result.value.pass) {
        Get-Content -LiteralPath $Output
        throw 'Packaged Inbox measurement failed'
    }
    $response.result.result.value | ConvertTo-Json -Depth 15
} finally {
    $deadline.Dispose()
    $socket.Dispose()
}
