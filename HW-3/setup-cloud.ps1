[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9-]{4,28}[a-z0-9]$')]
    [string]$ProjectId,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')]
    [string]$BucketName,
    [ValidatePattern('^[a-z]+-[a-z]+[0-9]+$')]
    [string]$Region = 'us-central1',
    [string]$Account,
    [string]$DataDirectory,
    [switch]$GenerateAndUpload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $DataDirectory) { $DataDirectory = Join-Path $PSScriptRoot 'data\pages' }
$gcloud = (Get-Command gcloud.cmd -ErrorAction Stop).Source
$configPath = Join-Path $PSScriptRoot 'config.json'
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$evidencePath = Join-Path $PSScriptRoot "evidence\setup-$runId"

# Windows PowerShell can treat harmless native stderr as a PowerShell error.
# Let the CLI display it, and determine success from the native exit code.
function Invoke-GcloudChecked {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $ErrorActionPreference = 'Continue'
    & $gcloud @Arguments
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "gcloud $($Arguments[0..([Math]::Min(2, $Arguments.Length - 1))] -join ' ') failed (exit $LASTEXITCODE)."
    }
}

function Invoke-Cloud {
    param([string[]]$Arguments)
    Invoke-GcloudChecked -Arguments ($Arguments + @("--project=$ProjectId", "--account=$Account", '--quiet'))
}

function Read-CloudJson {
    param([string[]]$Arguments)
    $raw = @(Invoke-Cloud -Arguments ($Arguments + @('--format=json')))
    if ($raw.Count -eq 0) { throw 'gcloud returned no JSON.' }
    return (($raw -join "`n") | ConvertFrom-Json)
}

function Save-Evidence {
    param([string]$Name, $Value)
    ConvertTo-Json -InputObject $Value -Depth 30 | Set-Content -LiteralPath (Join-Path $evidencePath $Name) -Encoding UTF8
}

if (-not $Account) {
    $active = @(Invoke-GcloudChecked -Arguments @('auth', 'list', '--filter=status:ACTIVE', '--format=value(account)'))
    if ($LASTEXITCODE -ne 0 -or $active.Count -ne 1) {
        throw 'Sign in first with gcloud.cmd auth login, then rerun this script.'
    }
    $Account = $active[0].Trim()
}
if ($Account -notmatch '^[^\s:@]+@[^\s:@]+$' -or $Account.EndsWith('.gserviceaccount.com')) {
    throw 'Use your human Google Cloud account for provisioning and the service account for the two apps.'
}

$inheritedImpersonation = @(Invoke-GcloudChecked -Arguments @('config', 'list', '--format=value(auth.impersonate_service_account)'))
if ($LASTEXITCODE -ne 0) { throw 'Could not read the gcloud impersonation setting.' }
if (($inheritedImpersonation -join '').Trim() -notin @('', '(unset)')) {
    throw 'Setup must run as your user. Run gcloud.cmd config unset auth/impersonate_service_account, then retry.'
}

Write-Host "Checking authentication and project $ProjectId as $Account..."
# This authenticated request must succeed before any cloud resource is changed.
$project = Read-CloudJson -Arguments @('projects', 'describe', $ProjectId)
if ($project.lifecycleState -ne 'ACTIVE') { throw 'The selected project is not ACTIVE.' }
$billing = Read-CloudJson -Arguments @('billing', 'projects', 'describe', $ProjectId)
if (-not $billing.billingEnabled) { throw 'Enable billing for the selected project before continuing.' }

if (Test-Path -LiteralPath $configPath) {
    $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($previous.projectId -ne $ProjectId -or $previous.region -ne $Region) {
        throw 'config.json belongs to a different project or region. Preserve and rename it before setting up another project.'
    }
    if ($BucketName -and $BucketName -ne $previous.bucketName) {
        throw 'BucketName differs from config.json. Preserve and rename that file before choosing a different bucket.'
    }
    $BucketName = $previous.bucketName
}
if (-not $BucketName) {
    $BucketName = "cs528-hw3-$ProjectId-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
}
if ($BucketName.Length -gt 63) { throw 'The generated bucket name exceeds 63 characters. Supply a shorter -BucketName.' }

$serviceAccountId = 'cs528-hw3-services'
$serviceAccountEmail = "$serviceAccountId@$ProjectId.iam.gserviceaccount.com"
$topicName = 'cs528-hw3-forbidden'
$subscriptionName = 'cs528-hw3-local'
$auditObject = 'hw3-logs/forbidden-requests.jsonl'
$dataPath = [IO.Path]::GetFullPath($DataDirectory)
$config = [ordered]@{
    projectId = $ProjectId
    projectNumber = [string]$project.projectNumber
    region = $Region
    bucketName = $BucketName
    objectPrefix = 'pages/'
    numFiles = 12000
    maxFileIndex = 11999
    dataDirectory = $dataPath
    serviceAccountEmail = $serviceAccountEmail
    topicName = $topicName
    topicPath = "projects/$ProjectId/topics/$topicName"
    subscriptionName = $subscriptionName
    subscriptionPath = "projects/$ProjectId/subscriptions/$subscriptionName"
    auditObject = $auditObject
    functionName = 'cs528-hw3-files'
    provisioningAccount = $Account
}
New-Item -ItemType Directory -Path $evidencePath -Force | Out-Null
# Persist names before provisioning so retries reuse the same resources.
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
Save-Evidence -Name 'resources.json' -Value $config
Save-Evidence -Name 'project.json' -Value $project

Start-Transcript -LiteralPath (Join-Path $evidencePath 'setup-transcript.txt') | Out-Null
try {
    if ($GenerateAndUpload) {
        & (Join-Path $PSScriptRoot 'prepare-data.ps1') -DataDirectory $dataPath
    }

    Write-Host 'Enabling the APIs needed for HW3 and the later function deployment...'
    Invoke-Cloud -Arguments @('services', 'enable',
        'serviceusage.googleapis.com', 'iam.googleapis.com', 'iamcredentials.googleapis.com',
        'storage.googleapis.com', 'pubsub.googleapis.com', 'cloudfunctions.googleapis.com',
        'run.googleapis.com', 'cloudbuild.googleapis.com', 'artifactregistry.googleapis.com')

    $accounts = @(Read-CloudJson -Arguments @('iam', 'service-accounts', 'list'))
    $matchingAccounts = @($accounts | Where-Object { $_.email -eq $serviceAccountEmail })
    if ($matchingAccounts.Count -eq 0) {
        Invoke-Cloud -Arguments @('iam', 'service-accounts', 'create', $serviceAccountId, '--display-name=CS528 HW3 shared runtime')
    } elseif ($matchingAccounts[0].PSObject.Properties['disabled'] -and $matchingAccounts[0].disabled) {
        throw 'The existing HW3 service account is disabled.'
    }

    $buckets = @(Read-CloudJson -Arguments @('storage', 'buckets', 'list'))
    $existingBuckets = @($buckets | Where-Object { ($_.name -replace '^gs://', '') -eq $BucketName })
    if ($existingBuckets.Count -eq 0) {
        Write-Host "Creating gs://$BucketName in $Region..."
        Invoke-Cloud -Arguments @('storage', 'buckets', 'create', "gs://$BucketName",
            "--location=$Region", '--default-storage-class=STANDARD',
            '--uniform-bucket-level-access', '--public-access-prevention', '--soft-delete-duration=0')
    }
    $bucket = Read-CloudJson -Arguments @('storage', 'buckets', 'describe', "gs://$BucketName", '--raw')
    if ($bucket.location.ToLowerInvariant() -ne $Region) { throw 'The existing bucket has a different location.' }
    if (-not $bucket.iamConfiguration.uniformBucketLevelAccess.enabled -or $bucket.iamConfiguration.publicAccessPrevention -ne 'enforced') {
        throw 'The existing bucket must have uniform bucket-level access and public access prevention enforced. No existing policy was changed.'
    }

    $topics = @(Read-CloudJson -Arguments @('pubsub', 'topics', 'list'))
    if (@($topics | Where-Object { $_.name -eq $config.topicPath }).Count -eq 0) {
        Invoke-Cloud -Arguments @('pubsub', 'topics', 'create', $topicName)
    }
    $subscriptions = @(Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'list'))
    if (@($subscriptions | Where-Object { $_.name -eq $config.subscriptionPath }).Count -eq 0) {
        Invoke-Cloud -Arguments @('pubsub', 'subscriptions', 'create', $subscriptionName,
            "--topic=$topicName", '--ack-deadline=60', '--message-retention-duration=7d')
    }
    $subscription = Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'describe', $subscriptionName)
    if ($subscription.topic -ne $config.topicPath) { throw 'The existing subscription points to a different topic.' }
    if ($subscription.pushConfig.PSObject.Properties['pushEndpoint']) { throw 'The existing subscription is a push subscription; a pull subscription is required.' }
    if ($subscription.PSObject.Properties['bigqueryConfig'] -or $subscription.PSObject.Properties['cloudStorageConfig']) {
        throw 'The existing subscription exports messages; a pull subscription is required.'
    }

    Write-Host 'Granting the shared runtime account access to the dataset, audit prefix, topic, and subscription...'
    $member = "serviceAccount:$serviceAccountEmail"
    Invoke-Cloud -Arguments @('storage', 'buckets', 'add-iam-policy-binding', "gs://$BucketName",
        "--member=$member", '--role=roles/storage.objectViewer', '--condition=None')
    $conditionPath = Join-Path $evidencePath 'audit-write-condition.json'
    [ordered]@{
        title = 'hw3-audit-prefix'
        description = 'Allow audit-file updates only under hw3-logs/ in the HW3 bucket.'
        expression = "resource.name.startsWith('projects/_/buckets/$BucketName/objects/hw3-logs/')"
    } | ConvertTo-Json | Set-Content -LiteralPath $conditionPath -Encoding UTF8
    Invoke-Cloud -Arguments @('storage', 'buckets', 'add-iam-policy-binding', "gs://$BucketName",
        "--member=$member", '--role=roles/storage.objectUser', "--condition-from-file=$conditionPath")
    Invoke-Cloud -Arguments @('pubsub', 'topics', 'add-iam-policy-binding', $topicName,
        "--member=$member", '--role=roles/pubsub.publisher')
    Invoke-Cloud -Arguments @('pubsub', 'subscriptions', 'add-iam-policy-binding', $subscriptionName,
        "--member=$member", '--role=roles/pubsub.subscriber')
    Invoke-Cloud -Arguments @('iam', 'service-accounts', 'add-iam-policy-binding', $serviceAccountEmail,
        "--member=user:$Account", '--role=roles/iam.serviceAccountTokenCreator', '--condition=None')
    Invoke-Cloud -Arguments @('iam', 'service-accounts', 'add-iam-policy-binding', $serviceAccountEmail,
        "--member=user:$Account", '--role=roles/iam.serviceAccountUser', '--condition=None')

    if ($GenerateAndUpload) {
        Write-Host 'Uploading pages/ with transport compression (objects remain ordinary HTML)...'
        Invoke-Cloud -Arguments @('storage', 'rsync', $dataPath, "gs://$BucketName/pages/",
            '--recursive', '--gzip-in-flight=html', '--content-type=text/html')
    } else {
        Write-Host 'Reusing the uploaded dataset; checking its existing filenames...'
    }

    $objects = @(Invoke-Cloud -Arguments @('storage', 'ls', "gs://$BucketName/pages/*.html"))
    $objects | Set-Content -LiteralPath (Join-Path $evidencePath 'uploaded-objects.txt') -Encoding UTF8
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    for ($index = 0; $index -lt 12000; $index++) { [void]$expected.Add("gs://$BucketName/pages/$index.html") }
    if ($objects.Count -ne 12000) { throw "Expected 12,000 uploaded pages; found $($objects.Count). Rerun to resume upload." }
    foreach ($object in $objects) {
        if (-not $expected.Remove($object.Trim())) { throw "Unexpected uploaded object: $object" }
    }
    if ($expected.Count -ne 0) { throw 'Some expected pages are missing from the bucket.' }

    Save-Evidence -Name 'bucket.json' -Value $bucket
    Save-Evidence -Name 'bucket-iam.json' -Value (Read-CloudJson -Arguments @('storage', 'buckets', 'get-iam-policy', "gs://$BucketName"))
    Save-Evidence -Name 'topic-iam.json' -Value (Read-CloudJson -Arguments @('pubsub', 'topics', 'get-iam-policy', $topicName))
    Save-Evidence -Name 'subscription.json' -Value $subscription
    Save-Evidence -Name 'subscription-iam.json' -Value (Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'get-iam-policy', $subscriptionName))
    Save-Evidence -Name 'service-account-iam.json' -Value (Read-CloudJson -Arguments @('iam', 'service-accounts', 'get-iam-policy', $serviceAccountEmail))

    # Read a real object as the shared service account; no access token is printed or saved.
    $downloadPath = Join-Path $evidencePath 'impersonated-page-0.html'
    $verified = $false
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        Invoke-GcloudChecked -AllowFailure -Arguments @('storage', 'cp', "gs://$BucketName/pages/0.html", $downloadPath,
            "--project=$ProjectId", "--account=$Account", "--impersonate-service-account=$serviceAccountEmail", '--quiet')
        if ($LASTEXITCODE -eq 0) { $verified = $true; break }
        if ($attempt -lt 6) {
            Write-Host 'Waiting 15 seconds for the new IAM grants to propagate...'
            Start-Sleep -Seconds 15
        }
    }
    if (-not $verified) { throw 'Impersonated object read failed. Inspect the IAM/authentication error, then rerun.' }
    $remoteHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash
    if ($GenerateAndUpload) {
        $localHash = (Get-FileHash -LiteralPath (Join-Path $dataPath '0.html') -Algorithm SHA256).Hash
        if ($localHash -ne $remoteHash) { throw 'The downloaded page does not match the generated page.' }
    }
    Save-Evidence -Name 'verification.json' -Value ([ordered]@{
        verifiedAtUtc = [DateTime]::UtcNow.ToString('o')
        uploadedPageCount = $objects.Count
        impersonatedIdentity = $serviceAccountEmail
        page0Sha256 = $remoteHash
        status = 'steps-1-and-2-complete'
    })
    Write-Host "Steps 1 and 2 complete. Configuration: $configPath"
    Write-Host "Setup evidence: $evidencePath"
    Write-Host "Bucket: gs://$BucketName; shared account: $serviceAccountEmail"
} finally {
    Stop-Transcript | Out-Null
}
