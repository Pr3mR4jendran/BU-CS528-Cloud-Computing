[CmdletBinding()]
param([switch]$SkipAuditCheck)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configuration = Read-Hw3Config
if (-not $configuration.PSObject.Properties['serviceUri']) { throw 'Deploy the function first with deploy.ps1.' }
$curl = (Get-Command curl.exe -ErrorAction Stop).Source
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$evidence = Join-Path $PSScriptRoot "evidence\demos-$runId"
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$payloadPath = Join-Path $evidence 'post-filename.json'
[IO.File]::WriteAllText($payloadPath, '{"filename":"pages/0.html"}', [Text.UTF8Encoding]::new($false))
$cases = @(
    @{ name = 'get'; method = 'GET'; path = '/pages/0.html'; expected = 200 },
    @{ name = 'post'; method = 'POST'; path = '/'; expected = 200 },
    @{ name = 'missing'; method = 'GET'; path = '/pages/12000.html'; expected = 404 }
)
foreach ($method in @('PUT', 'DELETE', 'HEAD', 'CONNECT', 'OPTIONS', 'TRACE', 'PATCH')) {
    $cases += @{ name = $method.ToLowerInvariant(); method = $method; path = '/pages/0.html'; expected = 501 }
}
foreach ($country in @('North Korea', 'Iran', 'Cuba', 'Myanmar', 'Iraq', 'Libya', 'Sudan', 'Zimbabwe', 'Syria')) {
    $cases += @{ name = 'denied-' + $country.ToLowerInvariant().Replace(' ', '-');
        method = 'GET'; path = '/pages/0.html'; expected = 400; country = $country }
}
$results = @()
foreach ($case in $cases) {
    $headersPath = Join-Path $evidence ($case.name + '.headers.txt')
    $bodyPath = Join-Path $evidence ($case.name + '.body')
    $arguments = @('--silent', '--show-error', '--http1.1', '--connect-timeout', '20', '--max-time', '60',
        '--dump-header', $headersPath, '--output', $bodyPath, '--write-out', '%{http_code}')
    if ($case.method -eq 'HEAD') { $arguments += '--head' }
    else { $arguments += @('--request', $case.method) }
    if ($case.method -eq 'POST') {
        $arguments += @('--header', 'Content-Type: application/json', '--data-binary', ('@' + $payloadPath))
    }
    if ($case.method -in @('PUT', 'PATCH')) { $arguments += @('--header', 'Content-Length: 0') }
    if ($case.ContainsKey('country')) { $arguments += @('--header', ('X-country: ' + $case.country)) }
    $url = $configuration.serviceUri.TrimEnd('/') + $case.path
    $arguments += $url
    $statusText = & $curl @arguments
    if ($LASTEXITCODE -ne 0) { throw "curl failed for $($case.name). Evidence: $evidence" }
    $status = [int]($statusText -join '')
    $headers = Get-Content -LiteralPath $headersPath -Raw
    $requestId = $null
    if ($headers -match '(?im)^x-request-id:\s*([^\r\n]+)') { $requestId = $Matches[1].Trim() }
    $countryName = if ($case.ContainsKey('country')) { $case.country } else { $null }
    $platformRejected = (-not $requestId) -and (($case.method -eq 'CONNECT' -and $status -eq 400) -or
        ($case.method -eq 'TRACE' -and $status -eq 405))
    $results += [ordered]@{ name = $case.name; method = $case.method; url = $url; country = $countryName;
        expected = $case.expected; actual = $status; passed = ($status -eq $case.expected);
        platformRejected = $platformRejected; eventId = $requestId }
    Write-Host ("{0,-22} HTTP {1} (expected {2})" -f $case.name, $status, $case.expected)
    if ($platformRejected) { Write-Host '  Google rejects this method before invoking the function; see local method evidence.' }
}
$summaryPath = Join-Path $evidence 'http-results.json'
$results | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
if (@($results | Where-Object { -not $_.passed -and -not $_.platformRejected }).Count) {
    throw "An HTTP case failed. Inspect $summaryPath"
}
$python = Get-Hw3Python
& $python (Join-Path $PSScriptRoot 'run-local-methods.py') --config (Join-Path $PSScriptRoot 'config.json') --evidence $evidence
if ($LASTEXITCODE -ne 0) { throw 'Local method demonstration failed.' }
if (-not $SkipAuditCheck) {
    & $python (Join-Path $PSScriptRoot 'verify-results.py') --config (Join-Path $PSScriptRoot 'config.json') --results $summaryPath
    if ($LASTEXITCODE -ne 0) { throw 'Audit verification failed. Ensure the local subscriber is running.' }
}
Write-Host "Cloud cases verified, including two documented platform rejections; all local methods return 501. Evidence: $evidence"
