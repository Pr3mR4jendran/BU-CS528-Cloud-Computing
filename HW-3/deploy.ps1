[CmdletBinding()]
param([switch]$SkipLocalChecks)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'common.ps1')
$configuration = Read-Hw3Config
if (-not $SkipLocalChecks) { & (Join-Path $PSScriptRoot 'setup-local.ps1') }
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$evidence = Join-Path $PSScriptRoot "evidence\deploy-$runId"
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$buildAccountId = 'cs528-hw3-build'
$buildEmail = "$buildAccountId@$($configuration.projectId).iam.gserviceaccount.com"
$accounts = @(Read-Hw3CloudJson @('iam', 'service-accounts', 'list') $configuration)
if (@($accounts | Where-Object { $_.email -eq $buildEmail }).Count -eq 0) {
    Invoke-Hw3Gcloud @('iam', 'service-accounts', 'create', $buildAccountId, '--display-name=CS528 HW3 function build') $configuration
}
Write-Host 'Configuring the dedicated build account...'
Invoke-Hw3Gcloud @('projects', 'add-iam-policy-binding', $configuration.projectId,
    "--member=serviceAccount:$buildEmail", '--role=roles/logging.logWriter', '--condition=None') $configuration | Out-Null
$repositories = @(Read-Hw3CloudJson @('artifacts', 'repositories', 'list', "--location=$($configuration.region)") $configuration)
if (@($repositories | Where-Object { $_.name.EndsWith('/gcf-artifacts') }).Count -eq 0) {
    Invoke-Hw3Gcloud @('artifacts', 'repositories', 'create', 'gcf-artifacts',
        "--location=$($configuration.region)", '--repository-format=docker') $configuration
}
Invoke-Hw3Gcloud @('artifacts', 'repositories', 'add-iam-policy-binding', 'gcf-artifacts',
    "--location=$($configuration.region)", "--member=serviceAccount:$buildEmail", '--role=roles/artifactregistry.writer') $configuration | Out-Null
$sourceBucket = "gcf-v2-sources-$($configuration.projectNumber)-$($configuration.region)"
$uploadBucket = "gcf-v2-uploads-$($configuration.projectNumber)-$($configuration.region).cloudfunctions.appspot.com"
$conditionPath = Join-Path $evidence 'build-source-condition.json'
[ordered]@{
    title = 'hw3-function-build-source'
    description = 'Read only objects in the regional Cloud Functions source and upload buckets.'
    expression = "resource.type == 'storage.googleapis.com/Object' && (resource.name.startsWith('projects/_/buckets/$sourceBucket/objects/') || resource.name.startsWith('projects/_/buckets/$uploadBucket/objects/'))"
} | ConvertTo-Json | Set-Content -LiteralPath $conditionPath -Encoding UTF8
Invoke-Hw3Gcloud @('projects', 'add-iam-policy-binding', $configuration.projectId,
    "--member=serviceAccount:$buildEmail", '--role=roles/storage.objectViewer', "--condition-from-file=$conditionPath") $configuration | Out-Null
Invoke-Hw3Gcloud @('iam', 'service-accounts', 'add-iam-policy-binding', $buildEmail,
    "--member=user:$($configuration.provisioningAccount)", '--role=roles/iam.serviceAccountUser', '--condition=None') $configuration | Out-Null

Write-Host 'Deploying the public HTTP function from the local source directory...'
$environment = "PROJECT_ID=$($configuration.projectId),BUCKET_NAME=$($configuration.bucketName),OBJECT_PREFIX=$($configuration.objectPrefix),PUBSUB_TOPIC=$($configuration.topicPath)"
Invoke-Hw3Gcloud @('functions', 'deploy', $configuration.functionName,
    '--gen2', "--region=$($configuration.region)", '--runtime=python313', '--entry-point=serve_file',
    '--trigger-http', '--allow-unauthenticated', '--ingress-settings=all',
    "--service-account=$($configuration.serviceAccountEmail)",
    "--build-service-account=projects/$($configuration.projectId)/serviceAccounts/$buildEmail",
    "--source=$(Join-Path $PSScriptRoot 'cloud-function')", "--set-env-vars=$environment",
    '--memory=256Mi', '--cpu=1', '--timeout=60s', '--min-instances=0', '--max-instances=2', '--concurrency=4') $configuration

$deployed = Read-Hw3CloudJson @('functions', 'describe', $configuration.functionName,
    '--gen2', "--region=$($configuration.region)") $configuration
if ($deployed.state -ne 'ACTIVE' -or $deployed.serviceConfig.serviceAccountEmail -ne $configuration.serviceAccountEmail) {
    throw 'The deployed function is not active under the required shared service account.'
}
$deployed | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $evidence 'function.json') -Encoding UTF8
$configuration | Add-Member -NotePropertyName serviceUri -NotePropertyValue $deployed.serviceConfig.uri -Force
$configuration | Add-Member -NotePropertyName buildServiceAccountEmail -NotePropertyValue $buildEmail -Force
$configuration | Add-Member -NotePropertyName lastDeployedAtUtc -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
$configuration | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Encoding UTF8
Write-Host "Deployed HTTP service: $($configuration.serviceUri)"
Write-Host "Runtime account: $($configuration.serviceAccountEmail)"
