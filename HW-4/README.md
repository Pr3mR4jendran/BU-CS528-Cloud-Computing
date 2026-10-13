# CS528 HW4: Python services on three Compute Engine VMs

Use [RUN-GUIDE.md](RUN-GUIDE.md) for Windows commands and the report evidence checklist.

The HTTP server reads the existing HW2-generated objects, explicitly handles GET/POST and all seven unsupported methods, checks the assignment's X-country header, writes structured error records directly to Cloud Logging, and publishes denied requests to Pub/Sub. The separate reporting VM prints denials and maintains a generation-checked audit object. The third VM runs the instructor's Linux client.

| File | Purpose |
|---|---|
| service.py, server.py | HTTP behavior and direct threaded HTTP transport |
| subscriber.py, audit.py | Pub/Sub consumer, stdout records, persistent deduplicated audit |
| deploy.py | Configurable provisioning, Linux startup scripts, evidence export, ownership-checked cleanup |
| demos.py | Actual curl requests; file-content hashes, response/status checks, log severities, and audit verification |
| run_client.py | Supplied Linux binary on its separate VM; verifies 100 responses, body types, request IDs, and persisted denials |
| run_stress.py, stress_worker.py | Bounded 1-4-client experiments with server/client Linux CPU, memory, and NIC sampling |
| plot_stress.py, requirements-performance.txt | Local measured-result summaries and PNG/PDF performance graphs |
| config.example.json | Portable resource configuration for a grader |
| requirements.txt | Pinned direct dependencies |
| tests/test_hw4.py | Real local HTTP tests and audit/deployment safety tests |

Deployment commands need Python and Google Cloud CLI. Copy config.example.json to config.json, set the project/account/existing dataset bucket, and place the instructor's x86-64 Linux http-client beside deploy.py. The application libraries are installed automatically on the VMs; the deployment driver itself uses only Python's standard library.

The first service uses a new HW4 identity with conditional read access to the dataset prefix, publish access to its topic, and project Logging Writer access. The reporter uses a different identity with subscription consume access and conditional read/write access only to the run's audit prefix. The client has no attached service account. Applications use metadata-issued credentials; no key files or ADC login are required. Only the provisioning user administers infrastructure. Each application runs as the unprivileged Linux user hw4app under systemd.

The services use Debian 12 x86-64; the client uses Ubuntu 24.04 x86-64 because the supplied PyInstaller binary's embedded Python library needs GLIBC_2.38. The web server uses f1-micro; the reporter and client use e2-micro. The server has a reserved regional external IP. HTTP uses TCP 8080. A dedicated VPC/subnet and narrowly targeted firewall rules allow HTTP to the server and SSH only through IAP. All three VMs have external addresses for package installation/API access; only the server's address is reserved. There is no load balancer or NAT gateway.

Run `python deploy.py up` to create the infrastructure and automatically start both services. Code and configuration are embedded in generated startup scripts under evidence/bootstrap-<owner>/, so first boot does not depend on GitHub credentials or a preexisting code bucket. Failed deployments remain tracked in state.json and can be resumed with the same command. Changing application code or configuration requires cleanup before a new deployment. A fresh `up` after `down` allocates a new address; always read serverUrl from the new state.json.

Run `python deploy.py down` after exporting screenshots/evidence. This is the script's automatic deletion option required by the instructor. It exports evidence, removes tracked VMs and their owned boot disks, removes scoped IAM grants, Pub/Sub resources, reserved address, firewall rules, subnet, VPC, and service accounts, deletes only this run's audit object and custom cloud log, then verifies resource absence. The original dataset bucket, dataset objects, HW3 infrastructure, project APIs, provider audit logs, SSH keys, and local evidence are retained. Global project services and SSH credentials are shared prerequisites, not disposable HW4 infrastructure. If audit export fails, chargeable infrastructure is still removed and the audit is retained; cleanup is reported incomplete until a retry succeeds. Ownership mismatches abort deletion. Keep state.json until cleanup has succeeded. After a confirmed service-account deletion, verification makes up to four checks with bounded delays to allow IAM list/describe results to converge. Persistent permission errors and remaining resources still fail cleanup. Rerunning down safely verifies already-absent resources and retries any remaining cleanup.

The supplied Linux binary is deliberately excluded from Git. The grader must supply the instructor-provided binary at the documented path. Follow [STRESS-GUIDE.md](STRESS-GUIDE.md) to run the full 1-4-client experiment and generate graphs from actual samples. The completed 180-second stages produced 25,374 responses (24,255 HTTP 200 and 1,119 expected HTTP 400), with zero unexpected failures. Four-client sampled server peaks were 35.05% CPU, 377.80 MiB host memory, 51.72 Mbit/s NIC receive and 51.76 Mbit/s transmit. Cleanup verified all 15 tracked resources absent and removed the runtime grants; the original dataset is retained.

The report is [report/CS528-HW4-Report.pdf](report/CS528-HW4-Report.pdf), with editable [TeX source](report/CS528-HW4-Report.tex), original screenshots, vector performance plots and compact verification summaries. See [report/README.md](report/README.md) to rebuild it. Actual billed cost is still awaiting evidence; follow [BILLING-EVIDENCE.md](BILLING-EVIDENCE.md) for screenshots and CSV exports before final submission. Local commits must be pushed to make the HW-4 GitHub link accessible to the grader.
