# Local Windows workflow

Use PowerShell in this repository's root directory. Prerequisites are Python 3.13 with the py launcher, Google Cloud CLI, curl.exe, and access to the configured Google Cloud project.

The first application runs as a cloud function. The subscriber runs locally on your laptop. Cloud Shell is not required.

## Existing uploaded dataset

The assignment deployment uses project bu-cs528-prem-project, region us-central1, and bucket cs528-hw3-bu-cs528-prem-project-1791250771. The bucket already contains pages/0.html through pages/11999.html. Standard setup reuses them.

    gcloud.cmd auth login
    gcloud.cmd config set project bu-cs528-prem-project
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-cloud.ps1 -ProjectId bu-cs528-prem-project -BucketName cs528-hw3-bu-cs528-prem-project-1791250771
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\verify-setup.ps1 -ProjectId bu-cs528-prem-project -BucketName cs528-hw3-bu-cs528-prem-project-1791250771

Setup configures the APIs, bucket, shared service account, Pub/Sub topic/subscription, and IAM. Verification checks those resources and the 12,000 filenames, tests impersonated access, and writes config.json. To check resources while the subscriber is already running, pass -SkipFunctionalChecks to verification.

Only if recreating the dataset is necessary, add -GenerateAndUpload to setup-cloud.ps1. The bundled unchanged HW2 generator is used with -n 12000 -m 325. Generation is not part of ordinary deployment.

## Install and deploy

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-local.ps1
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -SkipLocalChecks

The first helper creates .venv, installs dependencies, and runs unit tests. Deployment configures a separate build account and deploys the public Gen 2 function with the shared runtime account. It saves the current URL in config.json. Redeploy after changing cloud-function source.

## Start the local subscriber

In a separate terminal in this same directory:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-subscriber.ps1

Keep it running while generating denied requests. Ctrl+C stops the foreground service gracefully. Messages remain in the pull subscription while it is stopped, subject to seven-day retention.

For hidden background execution with captured output:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-subscriber.ps1 -Background
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop-subscriber.ps1

Use one subscriber for the demonstrations.

## curl and supplied client

Place the course-provided http-client.exe at the repository root, then run:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-demos.ps1
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-client.ps1

The demo captures GET/POST 200, missing-file 404, unsupported methods, all nine country denials, and local 501 checks. The cloud CONNECT/TRACE responses are documented platform exceptions. Audit verification correlates each denied event.

The client helper makes 100 HTTPS GET requests with -b none -w pages, maximum index 11999, and seed argument 0. Some requests receive expected 400 denials from randomly supplied country headers. The helper verifies response bodies and audit records.

For manual checks:

    $hw3 = Get-Content .\config.json -Raw | ConvertFrom-Json
    $baseUrl = $hw3.serviceUri
    curl.exe -i "$baseUrl/pages/0.html"
    Set-Content -LiteralPath .\evidence\post.json -Value '{"filename":"pages/0.html"}' -Encoding ASCII
    curl.exe -i -X POST -H 'Content-Type: application/json' --data-binary '@evidence/post.json' "$baseUrl/"
    curl.exe -i "$baseUrl/pages/12000.html"
    curl.exe -i -X PATCH "$baseUrl/pages/0.html"
    curl.exe -i -H 'X-country: Iran' "$baseUrl/pages/0.html"

## Browser

Open the service URL followed by /pages/0.html. In Developer Tools Console, run these same-origin requests with semicolons:

    console.log('GET:', (await fetch('/pages/0.html')).status);
    console.log('POST:', (await fetch('/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: 'pages/0.html'})})).status);
    console.log('Missing file:', (await fetch('/pages/12000.html')).status);
    console.log('Unsupported method:', (await fetch('/pages/0.html', {method: 'PATCH'})).status);
    console.log('Denied country:', (await fetch('/pages/0.html', {headers: {'X-country': 'Iran'}})).status);

Expected statuses are 200, 200, 404, 501, and 400.

## Audit and logs

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\show-audit.ps1 -Last 5
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\collect-evidence.ps1

In Cloud Logging expand the application stdout entries containing jsonPayload, not only the automatic HTTP request entries. The report needs the structured 404, 501, and 400 contents.

## Tests without cloud requests

After setup-local.ps1, run:

    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
