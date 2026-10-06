[CmdletBinding()]
param([ValidateRange(1, 1000)][int]$Last = 10)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configuration = Read-Hw3Config
$auditUri = "gs://$($configuration.bucketName)/$($configuration.auditObject)"
$lines = @(Invoke-Hw3Gcloud -Arguments @('storage', 'cat', $auditUri,
    "--impersonate-service-account=$($configuration.serviceAccountEmail)") -Configuration $configuration)
$records = @($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
    ForEach-Object { $_ | ConvertFrom-Json })
if (-not $records.Count) { throw 'The bucket audit file contains no events yet.' }
$evidence = Join-Path $PSScriptRoot ('evidence\audit-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$snapshotPath = Join-Path $evidence 'forbidden-requests.jsonl'
[IO.File]::WriteAllText($snapshotPath, ($lines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
Write-Host "Bucket audit: $auditUri"
Write-Host "Read as: $($configuration.serviceAccountEmail)"
Write-Host "Total accumulated events: $($records.Count)"
$recent = @($records | Sort-Object received_at | Select-Object -Last $Last)
$recent | Select-Object country, filename, method, status, received_at | Format-Table -AutoSize
$latest = $recent[-1]
Write-Host "Latest event ID: $($latest.event_id)"
Write-Host "Latest Pub/Sub message ID: $($latest.pubsub_message_id)"
Write-Host "Audit snapshot saved to $snapshotPath"
