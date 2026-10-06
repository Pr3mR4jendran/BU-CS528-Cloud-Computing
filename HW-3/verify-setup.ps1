[CmdletBinding()]
param(
    [string]$ProjectId = 'bu-cs528-prem-project',
    [string]$BucketName,
    [string]$Account,
    [switch]$SkipFunctionalChecks
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$gcloud = (Get-Command gcloud.cmd -ErrorAction Stop).Source
$configPath = Join-Path $PSScriptRoot 'config.json'
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$evidencePath = Join-Path $PSScriptRoot "evidence\verify-$runId"
$serviceAccountEmail = "cs528-hw3-services@$ProjectId.iam.gserviceaccount.com"
$topicName = 'cs528-hw3-forbidden'
$subscriptionName = 'cs528-hw3-local'
$checks = [Collections.Generic.List[object]]::new()

function Invoke-Cloud {
    param([string[]]$Arguments, [switch]$AsServiceAccount)
    # Native stderr is informational for commands such as impersonated reads.
    $ErrorActionPreference = 'Continue'
    $commandArguments = $Arguments + @("--project=$ProjectId", "--account=$Account", '--quiet')
    if ($AsServiceAccount) { $commandArguments += "--impersonate-service-account=$serviceAccountEmail" }
    & $gcloud @commandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud $($Arguments[0..([Math]::Min(2, $Arguments.Length - 1))] -join ' ') failed (exit $LASTEXITCODE)."
    }
}

function Read-CloudJson {
    param([string[]]$Arguments, [switch]$AsServiceAccount)
    $raw = @(Invoke-Cloud -Arguments ($Arguments + '--format=json') -AsServiceAccount:$AsServiceAccount)
    return (($raw -join "`n") | ConvertFrom-Json)
}

function Save-Json {
    param([string]$Name, $Value)
    ConvertTo-Json -InputObject $Value -Depth 30 | Set-Content -LiteralPath (Join-Path $evidencePath $Name) -Encoding UTF8
}

function Assert-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $checks.Add([ordered]@{ name = $Name; passed = $Passed; detail = $Detail })
    if (-not $Passed) { throw "$Name failed: $Detail" }
    Write-Host "PASS: $Name"
}

function Test-Binding {
    param($Policy, [string]$Role, [string]$Member, [string]$Expression)
    if (-not $Policy.PSObject.Properties['bindings']) { return $false }
    foreach ($binding in $Policy.bindings) {
        if ($binding.role -ne $Role -or $Member -notin $binding.members) { continue }
        if ($Expression) {
            if ($binding.PSObject.Properties['condition'] -and $binding.condition.expression -eq $Expression) { return $true }
        } elseif (-not $binding.PSObject.Properties['condition']) {
            return $true
        }
    }
    return $false
}

if (-not $Account) {
    $ErrorActionPreference = 'Continue'
    $active = @(& $gcloud auth list --filter=status:ACTIVE '--format=value(account)')
    $authExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($authExit -ne 0 -or $active.Count -ne 1) { throw 'Sign in with gcloud.cmd auth login first.' }
    $Account = $active[0].Trim()
}
if ($Account -notmatch '^[^\s:@]+@[^\s:@]+$') { throw 'Invalid provisioning account.' }
if (-not $BucketName -and (Test-Path -LiteralPath $configPath)) {
    $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($previous.projectId -ne $ProjectId) { throw 'config.json belongs to another project; supply its project explicitly.' }
    $BucketName = $previous.bucketName
}
if (-not $BucketName) {
    $buckets = @(Read-CloudJson -Arguments @('storage', 'buckets', 'list'))
    $matching = @($buckets | Where-Object { ($_.name -replace '^gs://', '') -like "cs528-hw3-$ProjectId-*" })
    if ($matching.Count -ne 1) { throw 'Could not uniquely identify the existing HW3 bucket. Supply -BucketName.' }
    $BucketName = $matching[0].name -replace '^gs://', ''
}
if ($BucketName -notmatch '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$') { throw 'Invalid bucket name.' }

New-Item -ItemType Directory -Path $evidencePath -Force | Out-Null
Start-Transcript -LiteralPath (Join-Path $evidencePath 'verification-transcript.txt') | Out-Null
$complete = $false
try {
    Write-Host "Verifying existing HW3 resources in $ProjectId from Windows PowerShell..."
    $project = Read-CloudJson -Arguments @('projects', 'describe', $ProjectId)
    $billing = Read-CloudJson -Arguments @('billing', 'projects', 'describe', $ProjectId)
    Assert-Check 'Project and billing' ($project.lifecycleState -eq 'ACTIVE' -and $billing.billingEnabled) 'Project must be active with billing enabled.'
    Save-Json 'project.json' $project

    $services = @(Read-CloudJson -Arguments @('services', 'list', '--enabled', '--format=json(config.name)'))
    $enabled = @($services | ForEach-Object { $_.config.name })
    $required = @('serviceusage.googleapis.com', 'iam.googleapis.com', 'iamcredentials.googleapis.com',
        'storage.googleapis.com', 'pubsub.googleapis.com', 'cloudfunctions.googleapis.com',
        'run.googleapis.com', 'cloudbuild.googleapis.com', 'artifactregistry.googleapis.com')
    $missing = @($required | Where-Object { $_ -notin $enabled })
    Assert-Check 'Required APIs' ($missing.Count -eq 0) ("Missing APIs: " + ($missing -join ', '))
    Save-Json 'enabled-apis.json' $enabled

    $bucket = Read-CloudJson -Arguments @('storage', 'buckets', 'describe', "gs://$BucketName", '--raw')
    Assert-Check 'Bucket location and access' ($bucket.location -eq 'US-CENTRAL1' -and $bucket.storageClass -eq 'STANDARD' -and
        $bucket.iamConfiguration.uniformBucketLevelAccess.enabled -and $bucket.iamConfiguration.publicAccessPrevention -eq 'enforced') 'Standard bucket in us-central1 with uniform IAM and public access prevention.'
    Save-Json 'bucket.json' $bucket
    $runtimeAccount = Read-CloudJson -Arguments @('iam', 'service-accounts', 'describe', $serviceAccountEmail)
    Assert-Check 'Shared service account' ($runtimeAccount.email -eq $serviceAccountEmail -and
        (-not $runtimeAccount.PSObject.Properties['disabled'] -or -not $runtimeAccount.disabled)) $serviceAccountEmail
    Save-Json 'service-account.json' $runtimeAccount

    $topic = Read-CloudJson -Arguments @('pubsub', 'topics', 'describe', $topicName)
    $subscription = Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'describe', $subscriptionName)
    $topicPath = "projects/$ProjectId/topics/$topicName"
    $pushEndpoint = $subscription.PSObject.Properties['pushConfig'] -and $subscription.pushConfig.PSObject.Properties['pushEndpoint']
    $exports = $subscription.PSObject.Properties['bigqueryConfig'] -or $subscription.PSObject.Properties['cloudStorageConfig']
    Assert-Check 'Topic and pull subscription' ($topic.name -eq $topicPath -and $subscription.topic -eq $topicPath -and
        -not $pushEndpoint -and -not $exports -and $subscription.ackDeadlineSeconds -eq 60 -and
        $subscription.messageRetentionDuration -eq '604800s') 'Pull subscription must use the HW3 topic, a 60-second ack deadline, and seven-day retention.'
    Save-Json 'topic.json' $topic
    Save-Json 'subscription.json' $subscription

    $bucketPolicy = Read-CloudJson -Arguments @('storage', 'buckets', 'get-iam-policy', "gs://$BucketName")
    $topicPolicy = Read-CloudJson -Arguments @('pubsub', 'topics', 'get-iam-policy', $topicName)
    $subscriptionPolicy = Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'get-iam-policy', $subscriptionName)
    $accountPolicy = Read-CloudJson -Arguments @('iam', 'service-accounts', 'get-iam-policy', $serviceAccountEmail)
    Save-Json 'bucket-iam.json' $bucketPolicy
    Save-Json 'topic-iam.json' $topicPolicy
    Save-Json 'subscription-iam.json' $subscriptionPolicy
    Save-Json 'service-account-iam.json' $accountPolicy
    $member = "serviceAccount:$serviceAccountEmail"
    $expression = "resource.name.startsWith('projects/_/buckets/$BucketName/objects/hw3-logs/')"
    Assert-Check 'Bucket read grant' (Test-Binding $bucketPolicy 'roles/storage.objectViewer' $member) 'Shared account must be able to read bucket objects.'
    Assert-Check 'Audit prefix write grant' (Test-Binding $bucketPolicy 'roles/storage.objectUser' $member $expression) 'Write grant must apply to hw3-logs/.'
    Assert-Check 'Pub/Sub publisher grant' (Test-Binding $topicPolicy 'roles/pubsub.publisher' $member) 'Shared account must publish to the topic.'
    Assert-Check 'Pub/Sub subscriber grant' (Test-Binding $subscriptionPolicy 'roles/pubsub.subscriber' $member) 'Shared account must consume the subscription.'
    Assert-Check 'User impersonation grant' (Test-Binding $accountPolicy 'roles/iam.serviceAccountTokenCreator' "user:$Account") 'Your account must be able to impersonate the shared account.'
    Assert-Check 'User deployment grant' (Test-Binding $accountPolicy 'roles/iam.serviceAccountUser' "user:$Account") 'Your account must be able to attach the shared account to the function.'

    Write-Host 'Checking the existing object inventory; no files are generated or uploaded...'
    $objects = @(Invoke-Cloud -Arguments @('storage', 'ls', "gs://$BucketName/pages/*.html"))
    $objects | Set-Content -LiteralPath (Join-Path $evidencePath 'uploaded-objects.txt') -Encoding UTF8
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    for ($index = 0; $index -lt 12000; $index++) { [void]$expected.Add("gs://$BucketName/pages/$index.html") }
    $validNames = $objects.Count -eq 12000
    foreach ($object in $objects) { if (-not $expected.Remove($object.Trim())) { $validNames = $false } }
    Assert-Check 'All 12,000 dataset filenames' ($validNames -and $expected.Count -eq 0) 'Expected exactly pages/0.html through pages/11999.html.'

    $pageInfo = Read-CloudJson -Arguments @('storage', 'objects', 'describe', "gs://$BucketName/pages/0.html")
    $pagePath = Join-Path $evidencePath 'impersonated-page-0.html'
    Invoke-Cloud -Arguments @('storage', 'cp', "gs://$BucketName/pages/0.html", $pagePath) -AsServiceAccount
    $page = [IO.File]::ReadAllBytes($pagePath)
    Assert-Check 'Impersonated dataset read' ($page.Length -eq [long]$pageInfo.size -and $pageInfo.content_type -eq 'text/html' -and
        [Text.Encoding]::UTF8.GetString($page).StartsWith('<!DOCTYPE html>')) 'Downloaded page size, type, and HTML header must match.'
    Save-Json 'page-0-metadata.json' $pageInfo

    if (-not $SkipFunctionalChecks) {
        Write-Host 'Testing audit create/overwrite/read with one small setup-verification object...'
        $probeUri = "gs://$BucketName/hw3-logs/setup-verification.json"
        $probePath = Join-Path $evidencePath 'audit-probe.json'
        $probeDownload = Join-Path $evidencePath 'audit-probe-downloaded.json'
        [ordered]@{ eventType = 'setup_verification'; runId = $runId; stage = 'created' } |
            ConvertTo-Json | Set-Content -LiteralPath $probePath -Encoding UTF8
        Invoke-Cloud -Arguments @('storage', 'cp', $probePath, $probeUri, '--content-type=application/json') -AsServiceAccount
        [ordered]@{ eventType = 'setup_verification'; runId = $runId; stage = 'overwritten' } |
            ConvertTo-Json | Set-Content -LiteralPath $probePath -Encoding UTF8
        Invoke-Cloud -Arguments @('storage', 'cp', $probePath, $probeUri, '--content-type=application/json') -AsServiceAccount
        Invoke-Cloud -Arguments @('storage', 'cp', $probeUri, $probeDownload) -AsServiceAccount
        Assert-Check 'Impersonated audit create, overwrite, and read' ((Get-FileHash -LiteralPath $probePath).Hash -eq
            (Get-FileHash -LiteralPath $probeDownload).Hash) $probeUri

        Write-Host 'Testing one Pub/Sub message using the shared service account...'
        $probeMessage = "CS528_HW3_SETUP_VERIFICATION:$runId"
        $published = Read-CloudJson -Arguments @('pubsub', 'topics', 'publish', $topicName,
            "--message=$probeMessage", '--attribute=event_type=setup_verification') -AsServiceAccount
        $messageId = [string]$published.messageIds[0]
        $received = $null
        for ($attempt = 1; $attempt -le 6; $attempt++) {
            $batch = @(Read-CloudJson -Arguments @('pubsub', 'subscriptions', 'pull', $subscriptionName,
                '--limit=10', '--no-auto-ack') -AsServiceAccount)
            foreach ($item in $batch) {
                if ($item.message.messageId -eq $messageId) { $received = $item; break }
            }
            if ($received) { break }
            if ($attempt -lt 6) { Start-Sleep -Seconds 5 }
        }
        Assert-Check 'Impersonated Pub/Sub publish and pull' ($null -ne $received) "Test message ID: $messageId"
        $decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($received.message.data))
        Assert-Check 'Pub/Sub test message contents' ($decoded -eq $probeMessage) 'Received body must match the published setup message.'
        # Acknowledge only this test message. Other pulled messages remain unacknowledged.
        Invoke-Cloud -Arguments @('pubsub', 'subscriptions', 'ack', $subscriptionName, "--ack-ids=$($received.ackId)") -AsServiceAccount
        Assert-Check 'Impersonated Pub/Sub acknowledgment' $true "Acknowledged test message $messageId."
        Save-Json 'pubsub-test.json' ([ordered]@{ messageId = $messageId; body = $probeMessage; acknowledged = $true })
    }

    $config = [ordered]@{
        projectId = $ProjectId
        projectNumber = [string]$project.projectNumber
        region = 'us-central1'
        bucketName = $BucketName
        objectPrefix = 'pages/'
        numFiles = 12000
        maxFileIndex = 11999
        serviceAccountEmail = $serviceAccountEmail
        topicName = $topicName
        topicPath = $topicPath
        subscriptionName = $subscriptionName
        subscriptionPath = "projects/$ProjectId/subscriptions/$subscriptionName"
        auditObject = 'hw3-logs/forbidden-requests.jsonl'
        functionName = 'cs528-hw3-files'
        provisioningAccount = $Account
    }
    # Keep deployment metadata when a later infrastructure check rewrites config.
    if (Test-Path -LiteralPath $configPath) {
        $previousConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($previousConfig.projectId -eq $ProjectId -and $previousConfig.bucketName -eq $BucketName) {
            foreach ($field in @('serviceUri', 'buildServiceAccountEmail', 'lastDeployedAtUtc')) {
                if ($previousConfig.PSObject.Properties[$field]) { $config[$field] = $previousConfig.$field }
            }
        }
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
    Save-Json 'resources.json' $config
    $complete = $true
    Write-Host "Existing infrastructure verified. Configuration: $configPath"
    Write-Host "Evidence: $evidencePath"
} finally {
    Save-Json 'verification.json' ([ordered]@{
        verifiedAtUtc = [DateTime]::UtcNow.ToString('o')
        complete = $complete
        functionalChecksRequested = -not $SkipFunctionalChecks
        serviceAccountEmail = $serviceAccountEmail
        checks = @($checks.ToArray())
    })
    Stop-Transcript | Out-Null
}
