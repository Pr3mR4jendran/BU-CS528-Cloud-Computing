# CS528 HW3: HTTP file service and Pub/Sub audit

A Python Gen 2 HTTP cloud function serves the HW2 HTML dataset from Cloud Storage. A second Python service runs on the local Windows laptop, consumes denied-country events from Pub/Sub, prints the denials, and appends them to an audit JSONL object in a separate prefix in the same bucket. Both use the same runtime service account.

See [RUN-GUIDE.md](RUN-GUIDE.md) for setup, deployment, and demonstration commands.

## Source layout

| Path | Purpose |
|---|---|
| cloud-function/main.py | Functions Framework entry point: serve_file |
| cloud-function/service.py | GET/POST handling, object reads, country rules, publishing, JSON print logs |
| subscriber/subscriber.py | Local pull subscriber and graceful shutdown |
| subscriber/authentication.py | Refreshable keyless service-account impersonation using the CLI login |
| subscriber/audit.py | Conditional JSONL updates, duplicate protection, and acknowledgements |
| hw2-generator/generate-content.py | Unchanged HW2 generator, included for optional regeneration |
| tests/test_services.py | HTTP, authentication, audit, and result-verification tests |
| setup-cloud.ps1 / verify-setup.ps1 | Provision resources and verify setup |
| setup-local.ps1 / deploy.ps1 | Install/test locally and deploy the cloud function |
| start-subscriber.ps1 / stop-subscriber.ps1 | Run and stop the local service |
| run-demos.ps1 / run-client.ps1 | curl demonstrations and supplied 100-request client |
| run-local-methods.py | Actual local curl checks for all unsupported methods |
| verify-results.py | Verify HTTP results against persisted audit events |
| show-audit.ps1 / collect-evidence.ps1 | Display the audit and export cloud configuration/logs |

## HTTP behavior

| Case | Application response |
|---|---|
| GET /pages/0.html | 200 with stored HTML |
| POST / with JSON filename field containing pages/0.html | 200 with stored HTML |
| Missing file | 404 with structured print |
| PUT, DELETE, HEAD, CONNECT, OPTIONS, TRACE, PATCH | 501 with structured print when the method reaches the function |
| GET/POST with assigned denied X-country | 400 after publishing an event |

The assignment country list is North Korea, Iran, Cuba, Myanmar, Iraq, Libya, Sudan, Zimbabwe, and Syria. Matching ignores case and extra whitespace. Country denial precedes bucket access; the unsupported-method check comes first. Only the dataset prefix is served.

Malformed POST payloads return 400. Other storage failures return 500; Pub/Sub publication failure returns 503. Application responses include an X-Request-Id matching the structured event ID.

**Cloud platform exception:** Google's serving layer rejects CONNECT and TRACE before the function executes. The recorded cloud responses were 400 and 405 respectively, while actual local requests to the same function returned 501. The demo scripts retain these cloud results as platform rejections rather than successful 501 checks. See [Google's official Cloud Run known issues](https://docs.cloud.google.com/run/docs/known-issues).

## Logging and persistence

The function uses simple JSON print statements with flush enabled. The platform ingests them as structured Cloud Logging entries; no Logging SDK is used.

The local subscriber reads and updates one JSONL bucket object. Generation-match preconditions prevent lost concurrent updates, and persisted event IDs prevent duplicate lines after redelivery. It acknowledges a valid event after persistence and printing; failed writes are negatively acknowledged for retry. Invalid messages are printed as errors and acknowledged to avoid an endless poison-message loop.

This small accumulating object meets the assignment's requirements. Large production streams would use partitioned objects or a database.

## Identity

The function uses its attached runtime account. The local subscriber passes explicit refreshable credentials to both clients. A refresh callback obtains a one-hour impersonated token through the existing human gcloud login and conservatively assigns a 55-minute expiry. No service-account key or gcloud auth application-default login is used.

The shared account has dataset read, audit-prefix write, topic publish, and subscription consume access. The human principal has Token Creator for impersonation. A separate build account builds the function.

## Files generated locally

Setup creates config.json, .venv, and evidence files. These are ignored by Git. config.example.json documents the resource identifiers without any tokens or account keys. Setup/verification should generate the real configuration.

The course-provided http-client.exe is not source code and is not included in this package. Place your supplied executable beside run-client.ps1 when running that demonstration.
