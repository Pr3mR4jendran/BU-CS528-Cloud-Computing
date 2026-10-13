# HW4 stress experiments and performance graphs

The experiment runs the instructor's supplied Linux binary on the existing client VM. It runs 1, 2, 3, and 4 client processes sequentially, for 180 seconds per level, with 15 seconds between levels. The workload portion is about 13 minutes; allow additional time for SSH setup and exports. It does not create additional VMs or delete infrastructure. Each level ends automatically. Do not start a second experiment or another http-client during the suite.

## Screenshot review

The latest bucket-storage-condition.png correctly shows the server's Storage Object Viewer condition restricted to objects/pages/. Screenshot 2026-10-13 040250.png correctly matches event 5d175253-b710-4b1f-b567-3dd94578de33 between reporter stdout and the audit record, including Iran, pages/0.html, and the Pub/Sub message ID.

The earlier reporter condition image was replaced by the new server condition image at the same filename. Before final cleanup, save the reporter's Storage Object User condition under a distinct filename showing the hw4-logs/<owner>/ restriction. The exported policy still contains both correct conditions.

The screenshot directory still lacks the rendered HTML/browser Network-panel captures and a standalone 100-request summary image described in EVIDENCE-REVIEW.md. Those are report capture tasks; they do not block these measurements. Keep both existing and new evidence.

## 1. Prepare locally

Run these in Windows PowerShell:

```powershell
Set-Location -LiteralPath 'C:\Users\premr\Desktop\BU Coursework\Fall 2026\CS 528 - Cloud Computing\Assignments\HW-4'
& .\.venv\Scripts\python.exe -m pip install -r .\requirements-performance.txt
if ($LASTEXITCODE -ne 0) { throw 'Graph dependency installation failed.' }
python .\deploy.py status
```

Your state was ready when the harness was implemented. If you have since run cleanup, run `python deploy.py up` again before the experiment. The runner reads the current state.json and endpoint automatically. It checks all three tracked VMs are running and owned by this deployment, proves the server sampler can read its process/counters, and refuses a pre-existing client workload or a second suite.

The plotting dependency is local only; it does not change the services or require a VM redeployment. The Linux worker uses the standard library already installed on both VMs.

## 2. Run the full experiment once

```powershell
$hw4StressTranscript = Join-Path $PWD ('evidence\stress-session-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
& .\.venv\Scripts\python.exe .\run_stress.py --seconds 180 --counts 1 2 3 4 --cooldown 15 2>&1 |
    Tee-Object -FilePath $hw4StressTranscript
if ($LASTEXITCODE -ne 0) { throw 'Stress experiment/export failed. Inspect the latest stress evidence before retrying or deleting VMs.' }
```

Let the process finish. It reports baseline, current client count, cooldown, and completion. It exports a new evidence/stress-<UTC timestamp>/ folder and writes summary.json, summary.csv, and results.md. An unexpected HTTP failure is a measured outcome; a failed harness/export is a different condition and makes the command fail.

Ctrl+C requests cancellation and attempts to export partial results. Workload deadlines also bound the remote processes. A cancelled run cannot be used as the full three-minute report experiment. Deleting VMs during a run loses evidence. The runner retries transient SSH status-read failures; if three reads fail, it stops and preserves partial evidence rather than treating unverified output as completion.

The suite uses the same fixed seed per client slot across load levels. Each supplied client issues sequential random file requests with a ten-second request timeout. Large verbose HTML bodies are discarded as they are read on the client VM; compact request/header output, response metadata, stderr, process exits, and deadline interruptions remain saved. Reader timestamps are output observations; no per-request latency values are claimed.

The supplied binary prints `Timeout received` and moves to its next request when a request times out. The worker records this as a `transport_failure` with reason `client_timeout` and continues the experiment. Such a timeout is a measured failure signal, not a reason to abort the harness; `request_timeouts` appears in the summaries. If the harness itself fails, its actual worker error and completed load levels are printed, and a partial summary is saved. A partial summary does not qualify as the full report experiment.

## 3. Generate and inspect graphs locally

```powershell
& .\.venv\Scripts\python.exe .\plot_stress.py --latest
if ($LASTEXITCODE -ne 0) { throw 'Graph generation failed or measurements are incomplete.' }
$hw4StressRun = Get-ChildItem -LiteralPath .\evidence -Directory -Filter 'stress-*' |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'summary.json') } |
    Sort-Object Name -Descending |
    Select-Object -First 1
$hw4StressSummary = Get-Content -LiteralPath (Join-Path $hw4StressRun.FullName 'summary.json') -Raw | ConvertFrom-Json
if (-not $hw4StressSummary.report_ready) { throw 'This is a smoke/partial run. The report needs 180 seconds at all four load levels.' }
Get-Content -LiteralPath (Join-Path $hw4StressRun.FullName 'results.md')
Invoke-Item -LiteralPath (Join-Path $hw4StressRun.FullName 'graphs\performance.png')
Invoke-Item -LiteralPath (Join-Path $hw4StressRun.FullName 'graphs\throughput-and-errors.png')
Invoke-Item -LiteralPath (Join-Path $hw4StressRun.FullName 'graphs\resource-timeseries.png')
```

`--latest` selects the most recent exported experiment, including a smoke run. The report_ready check above prevents using a short trial accidentally. To regenerate a particular run later, use `python plot_stress.py --folder '<full path to that stress folder>'` instead. Graphs can be regenerated after VM deletion from saved local files.

Each graph is saved as PNG and PDF:

- performance: server CPU mean/peak, host memory mean/peak and Python RSS peak, and NIC receive/transmit mean/peak versus client count.
- throughput-and-errors: completed response/download rates, unexpected failure signals with expected 400s excluded, and client VM CPU diagnostics.
- resource-timeseries: CPU, host memory, and NIC transmit bandwidth over the measured period at each load level.

Only measurements from the actual workload windows are summarized. Idle setup/cooldown samples are retained as baseline data. Missing measurement coverage, missing responses, or failure to reach the requested client count makes measurement verification fail. An incomplete experiment is refused for report graphs; a valid short smoke run gets an explicit smoke label.

## 4. Report evidence and interpretation

Capture the terminal's completion plus the results tables. Save the generated PNG/PDF figures directly for the report; screenshots of figures are unnecessary. Keep the full stress folder, its archives, the local transcript, and source scripts.

Key files are:

- deployment.json and vm-config.json: project/run identifiers, machine configurations, and attached identities.
- workload-config.json, stress_worker.py, and commands.jsonl: exact experiment setup and cloud command outcomes.
- client/run.json: actual workload start/end windows and client counts.
- server/server-metrics.jsonl and client/client-metrics.jsonl: timestamped raw measurements sampled approximately once per second.
- client/clients-N-slot-I.jsonl: requests/responses, process exits, and deadline interruptions for each slot.
- matching .stdout.txt/.stderr.txt: compact supplied-client output and errors. HTML body lines are intentionally omitted to limit disk growth.
- summary.json, summary.csv, results.md, and graphs/: derived measurements and report figures.

Expected country-denied 400 responses are excluded from capacity failures. Requests deliberately interrupted at a workload deadline are separately recorded and excluded. Unexpected statuses, invalid successful/denied responses, transport failures, and early client exits remain visible. A failure signal alone does not establish server CPU saturation: inspect stderr, server logs, client CPU, and backend behavior before attributing its cause.

The CPU percentage uses differences in Linux /proc/stat counters, excluding idle, I/O wait, and steal; steal/I/O wait are retained separately in raw data and summaries. Host memory used is MemTotal-MemAvailable, which accounts for reclaimable memory. Python RSS is reported separately. Bandwidth comes from receive/transmit byte-counter differences on the default NIC, and includes Cloud Storage/API traffic as well as HTTP. Peaks are peaks of approximately one-second samples, rather than instantaneous maxima. [Linux counter definitions](https://www.kernel.org/doc/html/latest/filesystems/proc.html).

The client VM is also sampled because four supplied-client processes, startup, and verbose-output handling can limit load generation. Interpret a high client CPU warning using both mean and peak CPU in the diagnostic figure. This experiment establishes behavior under the measured offered workload, not a universal maximum server capacity.

If no unexpected failures occur with four clients, report that no saturation was observed within the four-client limit, and use the four-client three-minute peaks from summary.json/results.md. Explain the measured CPU/memory/bandwidth trend using the time-series plots and system design. Do not invent results or call expected 400s overload failures.

## 5. Cost and final teardown

The graphs do not calculate billed cost. Retain workload timestamps and final cleanup timestamps, then obtain the project's Billing report for this homework window, accounting for server, reporter, client, disks, IPs, and relevant API usage. Identify gross charges, credits, and the net amount actually billed as applicable.

After the full experiment, graph verification, and missing browser/IAM captures:

```powershell
python .\deploy.py collect
if ($LASTEXITCODE -ne 0) { throw 'Final evidence export failed. Resolve before cleanup.' }
```

Then follow RUN-GUIDE section 9 for deletion, cleanup verification, and transcript closure. Keep the whole stress folder. The original dataset remains available for grading/recreation.

The short live trials used to verify this harness are explicitly marked smoke experiments. They are not the assignment's final three-minute results.
