$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot 'evidence\subscriber-process.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Host 'No background subscriber is recorded.'; return }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$expectedScript = Join-Path $PSScriptRoot 'subscriber\subscriber.py'
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.processId)" -ErrorAction SilentlyContinue
if (-not $processInfo) { Write-Host 'The subscriber has already stopped.'; return }
if ($state.script -ne $expectedScript -or -not $processInfo.CommandLine.Contains($expectedScript)) {
    throw 'The recorded PID belongs to another process; it was left alone.'
}
Set-Content -LiteralPath $state.stopFile -Value 'stop requested' -Encoding UTF8
for ($attempt = 0; $attempt -lt 35; $attempt++) {
    if (-not (Get-Process -Id $state.processId -ErrorAction SilentlyContinue)) {
        Write-Host 'Subscriber stopped gracefully.'
        return
    }
    Start-Sleep -Seconds 1
}
throw 'Subscriber is still finishing its current operation. Inspect its logs and retry shortly.'
