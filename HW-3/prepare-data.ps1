[CmdletBinding()]
param(
    [string]$DataDirectory,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $DataDirectory) { $DataDirectory = Join-Path $PSScriptRoot 'data\pages' }

$generator = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'hw2-generator\generate-content.py'))
if (-not (Test-Path -LiteralPath $generator -PathType Leaf)) {
    throw "The existing HW2 generator was not found: $generator"
}
$dataPath = [IO.Path]::GetFullPath($DataDirectory)
$manifestPath = Join-Path (Split-Path -Parent $dataPath) 'generation.json'
$generatorHash = (Get-FileHash -LiteralPath $generator -Algorithm SHA256).Hash

function Test-CompleteDataset {
    if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) { return $false }
    $files = @(Get-ChildItem -LiteralPath $dataPath -File)
    if ($files.Count -ne 12000) { return $false }
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in $files) { [void]$names.Add($file.Name) }
    for ($index = 0; $index -lt 12000; $index++) {
        if (-not $names.Contains("$index.html")) { return $false }
    }
    return $true
}

if (Test-CompleteDataset) {
    Write-Host "Reusing the complete dataset: $dataPath"
} else {
    $existing = @()
    if (Test-Path -LiteralPath $dataPath) {
        $existing = @(Get-ChildItem -LiteralPath $dataPath -Force)
    }
    if ($existing.Count -gt 0) {
        # An interrupted run of this script can be regenerated deterministically.
        if (-not (Test-Path -LiteralPath $manifestPath)) {
            throw 'The data directory is incomplete and has no generation manifest. Use a new empty directory.'
        }
        $previous = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ($previous.generatorSha256 -ne $generatorHash -or $previous.directory -ne $dataPath) {
            throw 'The incomplete dataset belongs to a different generator or directory. Use a new empty directory.'
        }
        if (@($existing | Where-Object { $_.PSIsContainer -or $_.Name -notmatch '^(0|[1-9][0-9]{0,4})\.html$' -or [int]$_.BaseName -ge 12000 }).Count -gt 0) {
            throw 'Unexpected content exists in the incomplete data directory. Use a new empty directory.'
        }
    }
    if (-not $Python) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            $Python = $launcher.Source
            $pythonPrefix = @('-3')
        } else {
            $interpreter = Get-Command python.exe -ErrorAction Stop
            $Python = $interpreter.Source
            $pythonPrefix = @()
        }
    } else {
        $pythonPrefix = @()
    }
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    $manifest = [ordered]@{
        directory = $dataPath
        generator = $generator
        generatorSha256 = $generatorHash
        numFiles = 12000
        maxRefs = 325
        complete = $false
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Write-Host 'Generating 12,000 pages with the unchanged HW2 generator (-n 12000 -m 325)...'
    Push-Location -LiteralPath $dataPath
    try {
        & $Python @pythonPrefix $generator -n 12000 -m 325
        if ($LASTEXITCODE -ne 0) { throw "HW2 generator failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
    if (-not (Test-CompleteDataset)) { throw 'Generation did not produce exactly 0.html through 11999.html.' }
    $manifest.complete = $true
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}

$files = @(Get-ChildItem -LiteralPath $dataPath -File)
$bytes = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host ('Validated {0:N0} pages, {1:N1} MiB, in {2}' -f $files.Count, ($bytes / 1MB), $dataPath)
