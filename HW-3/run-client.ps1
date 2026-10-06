[CmdletBinding()]
param([ValidateRange(1, 10000)][int]$Requests = 100)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configuration = Read-Hw3Config
if (-not $configuration.PSObject.Properties['serviceUri']) { throw 'Deploy the function first.' }
$client = Join-Path $PSScriptRoot 'http-client.exe'
if (-not (Test-Path -LiteralPath $client)) { throw 'The assignment http-client.exe is missing.' }
$serviceHost = ([Uri]$configuration.serviceUri).DnsSafeHost
$evidence = Join-Path $PSScriptRoot ('evidence\client-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$stdout = Join-Path $evidence 'client.stdout.txt'
$stderr = Join-Path $evidence 'client.stderr.txt'
$arguments = @('-d', $serviceHost, '-b', 'none', '-w', $configuration.objectPrefix.Trim('/'),
    '-n', "$Requests", '-i', "$($configuration.maxFileIndex)", '-p', '443', '-s', '-r', '0', '-t', '60', '-v')
[ordered]@{ executable = $client; arguments = $arguments; startedAtUtc = [DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $evidence 'command.json') -Encoding UTF8
Write-Host "Running $Requests requests against $serviceHost..."
$ErrorActionPreference = 'Continue'
& $client @arguments 1> $stdout 2> $stderr
$clientExitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($clientExitCode -ne 0) {
    Get-Content -LiteralPath $stderr -Tail 20
    throw "HTTP client failed (exit $clientExitCode). Evidence: $evidence"
}
$responses = @(Select-String -LiteralPath $stdout -Pattern '^\d{3} .+$')
$responses | ForEach-Object { $_.Line } | Group-Object | Select-Object Name, Count | Format-Table -AutoSize
$python = Get-Hw3Python
& $python (Join-Path $PSScriptRoot 'verify-results.py') --config (Join-Path $PSScriptRoot 'config.json') --client-output $stdout --requests $Requests --results (Join-Path $evidence 'http-results.json')
if ($LASTEXITCODE -ne 0) { throw "Client response verification failed. Ensure the subscriber is running. Evidence: $evidence" }
Write-Host "HTTP client completed. Full output: $stdout"
