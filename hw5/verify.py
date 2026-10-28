"""Run the required three curl requests and capture both SQL tables around each."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import os
import shlex
import subprocess
import uuid

import sqlalchemy as sa

from credentials import GcloudCredentials
from database import create_engine, failed_requests, requests, verify_schema

ROOT = Path(__file__).resolve().parent


def snapshot(engine, request_ids):
    result = {}
    with engine.connect() as connection:
        for table in (requests, failed_requests):
            result[table.name] = dict(
                total_count=connection.scalar(sa.select(sa.func.count()).select_from(table)),
                demonstration_rows=[dict(row) for row in connection.execute(sa.select(table).where(
                    table.c.request_id.in_(request_ids)).order_by(table.c.request_time)).mappings()])
    return result


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def demonstrate(engine, url, config, destination, *, curl=None):
    curl = curl or shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        raise RuntimeError("Install curl before running the demonstration.")
    destination.mkdir(parents=True, exist_ok=False)
    demonstration = "demo-" + uuid.uuid4().hex[:12]
    ids, steps = [], []
    cases = (("allowed", "United States", "0.html", 200), ("banned", "Iran", "0.html", 400),
             ("missing", "United States", "hw5-missing-" + uuid.uuid4().hex + ".html", 404))
    headers = {"X-client-IP": "203.0.113.22", "X-gender": "Female", "X-age": "17-25", "X-income": "20k-40k"}
    headers["X-time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save(destination / "manifest.json", dict(demonstration=demonstration, request_ids=ids, complete=False))
    for index, (name, country, filename, status) in enumerate(cases, 1):
        prefix = destination / f"{index}-{name}"
        save(prefix.with_suffix(".before.json"), snapshot(engine, ids))
        command = [curl, "--silent", "--show-error", "--max-time", "120", "--path-as-is",
            "--dump-header", str(prefix.with_suffix(".headers.txt")), "--output", str(prefix.with_suffix(".body")),
            "--write-out", "%{http_code}"]
        for key, value in {**headers, "X-country": country}.items():
            command += ["--header", f"{key}: {value}"]
        target = "/" + config["objectPrefix"].strip("/") + "/" + filename
        command.append(url.rstrip("/") + target)
        save(prefix.with_suffix(".command.json"), command)
        # A readable, copyable command for screenshots; no credentials are included.
        readable = ("& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
                    if os.name == "nt" else shlex.join(command))
        prefix.with_suffix(".command.txt").write_text(readable, encoding="utf-8")
        result = subprocess.run(command, text=True, capture_output=True, timeout=130)
        save(prefix.with_suffix(".curl.json"), dict(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if result.returncode or result.stdout.strip() != str(status):
            raise RuntimeError(f"{name}: expected HTTP {status}; inspect {prefix}.curl.json")
        response_headers = prefix.with_suffix(".headers.txt").read_text(encoding="utf-8")
        request_id = next((line.split(":", 1)[1].strip() for line in response_headers.splitlines()
                           if line.lower().startswith("x-request-id:")), None)
        if not request_id or request_id in ids:
            raise RuntimeError("The response did not include a unique X-Request-Id.")
        ids.append(request_id)
        after = snapshot(engine, ids)
        save(prefix.with_suffix(".after.json"), after)
        request_rows = after["requests"]["demonstration_rows"]
        failures = after["failed_requests"]["demonstration_rows"]
        if len(request_rows) != index or len(failures) != index - 1:
            raise RuntimeError("Demonstration table counts did not reconcile.")
        row = next(row for row in request_rows if row["request_id"] == request_id)
        if row["status_code"] != status or bool(row["is_banned"]) != (name == "banned"):
            raise RuntimeError("Persisted request does not match its response.")
        if status != 200 and not any(row["request_id"] == request_id and row["error_code"] == status for row in failures):
            raise RuntimeError("Missing linked failure row.")
        steps.append(dict(name=name, request_id=request_id, status_code=status,
                          requests=len(request_rows), failed_requests=len(failures)))
        save(destination / "manifest.json", dict(demonstration=demonstration, request_ids=ids,
             steps=steps, complete=index == 3, note="Counts select exact returned request IDs; global totals may include scanners/health checks."))
        print(f"{name}: HTTP {status}; demonstration requests={len(request_rows)}, failures={len(failures)}", flush=True)
    return destination / "manifest.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".state" / "runtime.json")
    parser.add_argument("--url", help="Default: current saved deployment URL, never an address literal in code.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gcloud-auth", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    url = args.url or json.loads((ROOT / ".state" / "deployment.json").read_text())["serverUrl"]
    destination = args.output or ROOT / "evidence" / ("demo-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    engine, connector = create_engine(config, credentials=GcloudCredentials() if args.gcloud_auth else None)
    try:
        verify_schema(engine)
        manifest = demonstrate(engine, url, config, destination)
        (ROOT / ".state" / "latest-demo.json").write_text(json.dumps(dict(manifest=str(manifest))), encoding="utf-8")
        print(manifest)
    finally:
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()
