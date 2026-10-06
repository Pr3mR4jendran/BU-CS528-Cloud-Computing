[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configuration = Read-Hw3Config
$evidence = Join-Path $PSScriptRoot ('evidence\service-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$function = Read-Hw3CloudJson @('functions', 'describe', $configuration.functionName,
    '--gen2', "--region=$($configuration.region)") $configuration
$function | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath (Join-Path $evidence 'function.json') -Encoding UTF8
$invocationPolicy = Read-Hw3CloudJson @('run', 'services', 'get-iam-policy', $configuration.functionName,
    "--region=$($configuration.region)") $configuration
$invocationPolicy | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $evidence 'invocation-iam.json') -Encoding UTF8
$filter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + $configuration.functionName + '" AND jsonPayload.event_type:*'
$logs = @(Read-Hw3CloudJson @('logging', 'read', $filter, '--freshness=2h', '--limit=1000', '--order=asc') $configuration)
ConvertTo-Json -InputObject $logs -Depth 40 | Set-Content -LiteralPath (Join-Path $evidence 'structured-cloud-logs.json') -Encoding UTF8
$logs | ForEach-Object { $_.jsonPayload } | Group-Object event_type | Select-Object Name, Count | Format-Table -AutoSize
Write-Host "Function, public invocation IAM, and structured print logs saved to $evidence"
Write-Host "Cloud console: https://console.cloud.google.com/run/detail/$($configuration.region)/$($configuration.functionName)/logs?project=$($configuration.projectId)"
