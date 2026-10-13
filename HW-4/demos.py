"""Required curl demonstrations with captured responses and cloud evidence."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from deploy import ROOT, Gcloud, collect


def main():
    state = json.loads((ROOT / "state.json").read_text())
    if state["status"] != "ready":
        raise SystemExit("Run deploy.py up successfully first.")
    config = state["config"]
    folder = ROOT / "evidence" / ("demos-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    folder.mkdir(parents=True)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl is required.")
    gc = Gcloud(config, folder)
    gc.run(["storage", "cp", "gs://" + config["bucketName"] + "/" + config["objectPrefix"] + "0.html", str(folder / "expected.html")])
    expected_hash = hashlib.sha256((folder / "expected.html").read_bytes()).hexdigest()
    payload = folder / "post.json"
    payload.write_text(json.dumps(dict(filename=config["objectPrefix"] + "0.html")), encoding="utf-8")
    file_path = "/" + config["objectPrefix"] + "0.html"
    cases = [("get", "GET", file_path, 200, []),
             ("post", "POST", "/", 200, ["-H", "Content-Type: application/json", "--data-binary", "@" + str(payload)]),
             ("missing", "GET", "/" + config["objectPrefix"] + str(config["maxFileIndex"] + 1) + ".html", 404, [])]
    for method in ("PUT", "DELETE", "HEAD", "CONNECT", "OPTIONS", "TRACE", "PATCH"):
        cases.append((method.lower(), method, file_path, 501, []))
    for country in ("North Korea", "Iran", "Cuba", "Myanmar", "Iraq", "Libya", "Sudan", "Zimbabwe", "Syria"):
        cases.append(("denied-" + country.lower().replace(" ", "-"), "GET", file_path, 400, ["-H", "X-country: " + country]))
    results = []
    for name, method, path, expected, flags in cases:
        headers, body = folder / (name + ".headers.txt"), folder / (name + ".body")
        command = [curl, "--silent", "--show-error", "--max-time", "40", "--dump-header", str(headers),
                   "--output", str(body), "--write-out", "%{http_code}"]
        command += ["--head"] if method == "HEAD" else ["--request", method]
        command += [*flags, state["serverUrl"] + path]
        run = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=50)
        status = int(run.stdout) if run.stdout.isdigit() else 0
        header_text = headers.read_text() if headers.exists() else ""
        event = re.search(r"^X-Request-Id:\s*(.+)$", header_text, re.I | re.M)
        event_id = event.group(1).strip() if event else None
        good = run.returncode == 0 and status == expected and event_id is not None
        if expected == 200:
            good = good and hashlib.sha256(body.read_bytes()).hexdigest() == expected_hash
        results.append(dict(name=name, method=method, status=status, expected=expected,
            passed=good, event_id=event_id, curlExit=run.returncode, stderr=run.stderr, command=command))
        print(f"{name:20} expected={expected} actual={status} passed={good}")
    (folder / "http-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    # Allow asynchronous Pub/Sub delivery and Cloud Logging ingestion to finish.
    deadline = time.monotonic() + 120
    while True:
        collect(state, folder / "cloud")
        entries = json.loads((folder / "cloud" / "cloud-logs.json").read_text())
        audit = [json.loads(line) for line in (folder / "cloud" / "forbidden-requests.jsonl").read_text().splitlines() if line.strip()]
        by_id = {entry.get("jsonPayload", {}).get("event_id"): entry for entry in entries}
        audit_ids = {entry["event_id"] for entry in audit}
        checks = []
        for result in results:
            if result["expected"] == 200:
                continue
            severity = {404: "WARNING", 501: "ERROR", 400: "CRITICAL"}[result["expected"]]
            entry = by_id.get(result["event_id"], {})
            passed = entry.get("severity") == severity and entry.get("jsonPayload", {}).get("status") == result["expected"]
            if result["expected"] == 400:
                passed = passed and result["event_id"] in audit_ids
            checks.append(dict(name=result["name"], event_id=result["event_id"], severity=severity, passed=passed))
        if all(check["passed"] for check in checks) or time.monotonic() >= deadline:
            break
        time.sleep(10)
    (folder / "cloud-verification.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print("Evidence: " + str(folder))
    if not all(result["passed"] for result in results) or not all(check["passed"] for check in checks):
        raise SystemExit("Some checks failed. Review the JSON results before describing the demo as successful.")
    print("All curl statuses, successful file hashes, severities, and forbidden-country audit records verified.")


if __name__ == "__main__":
    main()
