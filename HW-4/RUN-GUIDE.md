# HW4 first-stage run guide and report evidence

The source is in this directory. These are local Windows PowerShell commands. The deployment driver is also portable to Cloud Shell using `python3 deploy.py ...` after copying the code, config, and instructor-provided Linux client there.

## 1. Open the directory and save a terminal transcript

```powershell
Set-Location -LiteralPath 'C:\Users\premr\Desktop\BU Coursework\Fall 2026\CS 528 - Cloud Computing\Assignments\HW-4'
New-Item -ItemType Directory -Path .\evidence, .\screenshots -Force | Out-Null
Start-Transcript -LiteralPath (Join-Path $PWD ('evidence\session-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt'))
gcloud.cmd auth list --filter=status:ACTIVE --format='value(account)'
Get-Content .\config.json
python .\deploy.py plan
```

If authentication has expired, run `gcloud.cmd auth login` interactively and select your BU Google account. Your recent login already satisfies this step. Do not print access tokens or download service-account keys for evidence.

Your config points to project bu-cs528-prem-project, region us-central1, zone us-central1-a, and the existing private bucket cs528-hw3-bu-cs528-prem-project-1791250771. This bucket contains the HW2-generated dataset reused by HW3. The uploaded pages are reused; setup does not regenerate or reupload them. A grader can use config.example.json with their project and existing dataset bucket instead.

The provisioning user needs permission to enable services, create/manage Compute resources and service accounts, modify project/resource IAM, administer Pub/Sub, and read the existing dataset. Your user currently has project Owner; VM runtime accounts receive only the scoped roles documented below. For IAP SSH, a non-Owner grader additionally needs IAP-secured Tunnel User and the relevant Compute administration and Service Account User permissions. An organization policy that requires OS Login needs a corresponding deployment adjustment; this script uses instance metadata SSH access.

**Capture:** the config/plan output showing the chosen project, dataset prefix, three machines, and resource names. Retain the transcript. Authentication itself does not need a screenshot.

## 2. Verify local code

The local virtual environment has already been created. To reproduce it later:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests exercise actual HTTP sockets for GET, three POST formats, all seven unsupported methods, all nine forbidden countries, log severities, and storage/publish failures. Other checks cover audit deduplication, acknowledgement behavior, startup generation, cleanup scope, missing-resource handling, and foreign ownership refusal. Local tests use mocked Google clients; they are not evidence of cloud IAM or successful deployment.

**Retain:** test output in your transcript. An optional screenshot of the passing test summary is useful implementation evidence.

## 3. Create infrastructure and automatically start the services

```powershell
python .\deploy.py up
if ($LASTEXITCODE -ne 0) { throw 'Deployment incomplete. Keep state.json and inspect the evidence folder.' }
$hw4 = Get-Content .\state.json -Raw | ConvertFrom-Json
$hw4.serverUrl
python .\deploy.py status
gcloud.cmd compute instances list --project=$($hw4.config.projectId) --filter='name~^cs528-hw4-' --format='table(name,zone.basename(),machineType.basename(),status,networkInterfaces[0].networkIP,networkInterfaces[0].accessConfigs[0].natIP)'
gcloud.cmd compute addresses describe cs528-hw4-ip --region=us-central1 --project=$($hw4.config.projectId)
```

The single `up` command creates two service accounts, a dedicated VPC/subnet, two firewall rules, a reserved regional IP, a topic/subscription, three disks, and three VMs. It embeds source/config in startup metadata, starts both services using systemd, installs the supplied client, checks an actual bucket-backed HTTP request, waits for the reporter's ready message, captures client help, and exports initial evidence.

If the command fails, use `python deploy.py collect` for diagnostics and `python deploy.py down` for cleanup. You can retry `up` with the same code/config to resume an incomplete deployment. Do not delete state.json or manually adopt resources with the same names. Application code/config changes require a new deployment after cleanup.

**Capture:** Compute Engine's VM list showing all three running VMs. On the web server detail page show f1-micro, the attached HW4 server account, and its IP. In VPC Network > IP addresses show that the web server IP is reserved/static. Save the output above and evidence/up-*/resources.json.

**Capture IAM:** the server's conditional Storage Object Viewer binding for pages/, its topic Pub/Sub Publisher binding, and its project Logging Writer binding. Show the reporter's subscription Pub/Sub Subscriber binding and its conditional Storage Object User binding restricted to hw4-logs/<owner>/. The client should have no attached account. The driver exports bucket/topic/subscription IAM snapshots; VM snapshots include attached identities/scopes.

## 4. Verify automatic startup and stdout

```powershell
gcloud.cmd compute ssh cs528-hw4-server --zone=us-central1-a --project=bu-cs528-prem-project --tunnel-through-iap --command='sudo systemctl is-enabled cs528-hw4-server'
gcloud.cmd compute ssh cs528-hw4-server --zone=us-central1-a --project=bu-cs528-prem-project --tunnel-through-iap --command='sudo systemctl status cs528-hw4-server --no-pager'
gcloud.cmd compute ssh cs528-hw4-reporter --zone=us-central1-a --project=bu-cs528-prem-project --tunnel-through-iap --command='sudo systemctl is-enabled cs528-hw4-reporter'
gcloud.cmd compute ssh cs528-hw4-reporter --zone=us-central1-a --project=bu-cs528-prem-project --tunnel-through-iap --command='sudo systemctl status cs528-hw4-reporter --no-pager'
```

**Capture:** enabled + active (running) output for each service, with the VM name visible. Keep the generated evidence/bootstrap-*/server.sh and reporter.sh as proof of what was supplied on VM creation. The evidence collection also exports serial output for startup diagnostics and each service's journal.

## 5. Run and capture HTTP behavior

```powershell
python .\demos.py
if ($LASTEXITCODE -ne 0) { throw 'HTTP/log/audit checks failed. Inspect the demo evidence before reporting success.' }
$hw4 = Get-Content .\state.json -Raw | ConvertFrom-Json
curl.exe -i "$($hw4.serverUrl)/pages/0.html"
Set-Content -LiteralPath .\evidence\post.json -Value '{"filename":"pages/0.html"}' -Encoding ASCII
curl.exe -i -X POST -H 'Content-Type: application/json' --data-binary '@evidence/post.json' "$($hw4.serverUrl)/"
curl.exe -i "$($hw4.serverUrl)/pages/12000.html"
curl.exe -i -X PATCH "$($hw4.serverUrl)/pages/0.html"
curl.exe -i -H 'X-country: Iran' "$($hw4.serverUrl)/pages/0.html"
```

The automated demo uses actual curl for GET, POST, missing-file 404, all seven 501 methods, and all nine forbidden countries. It compares GET/POST response hashes with the original bucket object and verifies matching Cloud Logging severities and persisted denial events. Unlike HW3's serving gateway, the direct VM endpoint can forward CONNECT and TRACE to our handler. Do not classify an unexpected 500/503 or network failure as a successful required case.

**Capture:** terminal output showing 200, 404, 501, and 400. Retain evidence/demos-*/http-results.json, individual response headers/bodies, cloud-verification.json, and the cloud/ exports. X-Request-Id joins a response to jsonPayload.event_id in Cloud Logging and event_id in the audit.

## 6. Browser screenshots required for the report

Open the URL printed by `$hw4.serverUrl` followed by `/pages/0.html`. This should display the uploaded HTML. Open browser Developer Tools > Network, preserve the request list, and use the Console on this same server page:

```javascript
console.log('GET existing:', (await fetch('/pages/0.html')).status);
console.log('POST existing:', (await fetch('/', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: 'pages/0.html'})})).status);
console.log('GET missing:', (await fetch('/pages/12000.html')).status);
console.log('PATCH unsupported:', (await fetch('/pages/0.html', {method: 'PATCH'})).status);
console.log('GET forbidden:', (await fetch('/pages/0.html', {headers: {'X-country': 'Iran'}})).status);
```

**Capture five cases:** GET 200 with displayed content; POST 200 with its filename payload; missing-file 404; PATCH 501; Iran 400 with the X-country header. Show the real URL, method, status, request details, and response X-Request-Id where possible. A browser address bar issues GET only, so use the Network panel for POST/PATCH. Keep requests on the server's origin because OPTIONS must return 501, making cross-origin preflight unsuitable for this demo.

## 7. Cloud Logging and reporter evidence

In Logs Explorer select this project and use the deployed log name from `$hw4.runtime.logName`:

```text
logName="projects/bu-cs528-prem-project/logs/REPLACE_WITH_RUNTIME_LOG_NAME"
jsonPayload.status=(400 OR 404 OR 501)
```

**Capture three expanded JSON entries:** 404 with top-level severity WARNING; 501 with ERROR; forbidden 400 with CRITICAL. Show event_id, method, filename, country, and status. Successful requests are kept in the VM journal, while required error records are delivered explicitly with the Cloud Logging client library.

```powershell
gcloud.cmd compute ssh cs528-hw4-reporter --zone=us-central1-a --project=bu-cs528-prem-project --tunnel-through-iap --command='sudo journalctl -u cs528-hw4-reporter --no-pager -n 30'
$hw4 = Get-Content .\state.json -Raw | ConvertFrom-Json
gcloud.cmd storage cat "gs://$($hw4.config.bucketName)/$($hw4.runtime.auditObject)" --project=$($hw4.config.projectId)
python .\deploy.py collect
```

**Capture:** the reporter journal showing its printed denial message, country, filename, and event_id; the matching audit record in the bucket. The new evidence/collect-* directory retains snapshots, cloud logs, audit JSONL, serial output, and service journals. Keep full exports alongside screenshots.

## 8. The client VM and later performance work

The deployment installs /opt/cs528-hw4/http-client on the client VM and saves its actual help output under evidence/up-*/http-client-help.txt. The 100-request demonstration must run there, not on the laptop. The accompanying run_client.py helper captures and verifies that workload. After this demonstration, follow [STRESS-GUIDE.md](STRESS-GUIDE.md) for controlled 1/2/3/4-client, three-minute workloads, CPU/memory/bandwidth sampling, and PNG/PDF performance graphs before final cleanup. Cost accounting still requires Billing evidence. Never launch more than four concurrent clients; expected forbidden-country 400 responses are excluded from capacity failures.

```powershell
& .\.venv\Scripts\python.exe .\run_client.py
if ($LASTEXITCODE -ne 0) { throw 'The supplied client failed response/body/audit verification.' }
```

**Capture:** the 100-response summary showing successful downloads and expected 400 denials. Retain evidence/client-100-*/command.json, client.stdout.txt, client.stderr.txt, http-results.json, verification.json, and cloud/ exports. This is one client process, not the stress test.

## 9. Export evidence and delete resources when finished

`python deploy.py down` is the automatic deletion option in the same provisioning script, as confirmed by the instructor's follow-up. It performs the deletions and verification without manual Console deletion. It can be rerun after a partial cleanup. A successfully deleted service account can briefly remain in IAM's list view; the driver retries its absence checks with bounded delays and keeps reporting genuine permission errors or remaining resources.

Capture the browser and Cloud Console screenshots before cleanup. Cleanup deletes the run's custom Cloud Logging entries and its audit object after export.
Complete STRESS-GUIDE.md before final teardown if you intend to finish the assignment in this deployment. If pausing earlier, recreate the VMs later to perform the missing measurements.

```powershell
python .\deploy.py collect
python .\deploy.py down
if ($LASTEXITCODE -ne 0) { throw 'Cleanup incomplete. Read evidence/down-*/cleanup.json and rerun down after fixing the reported issue.' }
python .\deploy.py status
Stop-Transcript
```

**Capture:** CLEANUP VERIFIED, cleanup.json with status deleted and empty remaining/failures, and the status output showing each tracked resource absent. The original bucket/data and local evidence remain. Service-account deletion can leave Google-managed deleted-account records temporarily; these are provider lifecycle records, not running VMs or active runtime grants. Enabled APIs and provider audit logs are intentionally retained for the existing course project.

To prove grading reproducibility, run `python deploy.py up` again after successful cleanup. Read the new serverUrl, verify both automatic services, repeat a small demo, then collect and down again. Do not report delete/recreate verification until it has actually been run.

Document the up/ready/down timestamps from state.json and the exported deployment snapshots. The completed full stress suite and verified cleanup are recorded in the report. Actual billed cost still needs the screenshots/CSVs listed in [BILLING-EVIDENCE.md](BILLING-EVIDENCE.md).
