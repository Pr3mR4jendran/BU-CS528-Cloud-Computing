[CmdletBinding()]
param([switch]$Background, [int]$MaxMessages = 0)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$pythonPath = Get-Hw3Python
$configuration = Read-Hw3Config
$scriptPath = Join-Path $PSScriptRoot 'subscriber\subscriber.py'
$configPath = Join-Path $PSScriptRoot 'config.json'
if (-not $Background) {
    & $pythonPath -u $scriptPath --config $configPath --max-messages $MaxMessages
    if ($LASTEXITCODE -ne 0) { throw 'Subscriber exited with an error.' }
    return
}
$evidence = Join-Path $PSScriptRoot 'evidence'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$statePath = Join-Path $evidence 'subscriber-process.json'
if (Test-Path -LiteralPath $statePath) {
    $previous = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $existingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($previous.processId)" -ErrorAction SilentlyContinue
    if ($existingProcess -and $existingProcess.CommandLine.Contains($scriptPath)) {
        Write-Host "Subscriber is already running (PID $($previous.processId)). Output: $($previous.stdout)"
        return
    }
}
$stopPath = Join-Path $evidence 'subscriber.stop'
if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$stdout = Join-Path $evidence "subscriber-$runId.stdout.log"
$stderr = Join-Path $evidence "subscriber-$runId.stderr.log"
$arguments = @('-u', ('"' + $scriptPath + '"'), '--config', ('"' + $configPath + '"'),
    '--max-messages', "$MaxMessages", '--stop-file', ('"' + $stopPath + '"'))
$launchArguments = @{
    FilePath = $pythonPath; ArgumentList = $arguments; WorkingDirectory = $PSScriptRoot
    WindowStyle = 'Hidden'; RedirectStandardOutput = $stdout; RedirectStandardError = $stderr; PassThru = $true
}
$process = Start-Process @launchArguments
[ordered]@{ processId = $process.Id; script = $scriptPath; stdout = $stdout; stderr = $stderr;
    stopFile = $stopPath; startedAtUtc = [DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    if ($process.HasExited) {
        Get-Content -LiteralPath $stderr
        throw 'Subscriber exited during startup.'
    }
    if (Test-Path -LiteralPath $stdout) {
        $text = Get-Content -LiteralPath $stdout -Raw
        if ($text -and $text.Contains('subscriber_ready')) {
            Write-Host "Subscriber ready on your laptop (PID $($process.Id))."
            Write-Host "Output: $stdout"
            return
        }
    }
    Start-Sleep -Seconds 1
}
throw "Subscriber startup is still pending. Inspect $stdout and $stderr."
