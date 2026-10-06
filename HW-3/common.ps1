$script:Hw3Root = $PSScriptRoot

function Read-Hw3Config {
    $path = Join-Path $script:Hw3Root 'config.json'
    if (-not (Test-Path -LiteralPath $path)) { throw 'config.json is missing. Run verify-setup.ps1 first.' }
    return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
}

function Invoke-Hw3Gcloud {
    param([string[]]$Arguments, $Configuration)
    $ErrorActionPreference = 'Continue'
    $executable = (Get-Command gcloud.cmd -ErrorAction Stop).Source
    & $executable @Arguments "--project=$($Configuration.projectId)" "--account=$($Configuration.provisioningAccount)" --quiet
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($Arguments[0]) $($Arguments[1]) failed (exit $LASTEXITCODE)." }
}

function Read-Hw3CloudJson {
    param([string[]]$Arguments, $Configuration)
    $output = @(Invoke-Hw3Gcloud -Arguments ($Arguments + '--format=json') -Configuration $Configuration)
    return (($output -join "`n") | ConvertFrom-Json)
}

function Get-Hw3Python {
    $path = Join-Path $script:Hw3Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $path)) { throw 'Run setup-local.ps1 to create the Python environment first.' }
    return $path
}
