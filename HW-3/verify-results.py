"""Check that every denied curl request reached the persistent bucket audit."""
import argparse
import json
from pathlib import Path
import re
import sys
import time

from google.auth.transport.requests import Request
from google.api_core.exceptions import NotFound
from google.cloud import storage

sys.path.insert(0, str(Path(__file__).resolve().parent / "subscriber"))
from authentication import impersonated_credentials
sys.path.insert(0, str(Path(__file__).resolve().parent / "cloud-function"))
from service import FORBIDDEN_COUNTRIES


def client_results(path, requests):
    raw = path.read_bytes()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    results = []
    current = None
    for line in raw.decode(encoding).splitlines():
        match = re.match(r"^Requesting\s+(\S+)\s+from\s+(\S+)\s+(\d+)", line)
        if match:
            current = dict(name=f"client-{len(results)+1}", method="GET", filename=match[1].lstrip("/"),
                           url=f"https://{match[2]}{match[1]}", country=None, actual=None, eventId=None)
            results.append(current)
        elif current is not None:
            status = re.match(r"^(\d{3})\s+", line)
            request_id = re.match(r"^x-request-id:\s*(\S+)", line, re.IGNORECASE)
            if status:
                current["actual"] = int(status[1])
            elif request_id:
                current["eventId"] = request_id[1]
            elif line.startswith("b'") or line.startswith('b"'):
                current["bodyIsHtml"] = line[2:].startswith("<!DOCTYPE html>")
                current["bodyIsDenial"] = line[2:].startswith("Permission denied.")
    if len(results) != requests:
        raise RuntimeError(f"Expected {requests} client responses; parsed {len(results)}.")
    for result in results:
        status = result["actual"]
        if status not in (200, 400) or not result["eventId"]:
            raise RuntimeError(f"Unexpected client response for {result['filename']}: HTTP {status}.")
        if not result.get("bodyIsHtml" if status == 200 else "bodyIsDenial"):
            raise RuntimeError("Client response body did not match its status.")
    return results


def wait_for_audit(bucket, object_name, expected, timeout=60, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        try:
            # A Blob remembers the generation from its previous download. A fresh
            # Blob on every poll ensures this lookup follows the current object.
            text = bucket.blob(object_name).download_as_text(timeout=20)
        except NotFound:
            text = ""
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
        ids = [entry["event_id"] for entry in entries]
        missing = expected.difference(ids)
        if not missing:
            if any(ids.count(event_id) != 1 for event_id in expected):
                raise RuntimeError("A test request appears more than once in the audit log.")
            return text, entries
        if clock() >= deadline:
            raise RuntimeError(f"{len(missing)} denied events were not persisted within {timeout} seconds.")
        sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--client-output", type=Path)
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if args.client_output:
        results = client_results(args.client_output, args.requests)
        args.results.write_text(json.dumps(results, indent=2), encoding="utf-8")
    else:
        results = json.loads(args.results.read_text(encoding="utf-8-sig"))
    expected = {case["eventId"] for case in results if case["actual"] == 400 and not case.get("platformRejected")}
    if None in expected:
        raise RuntimeError("A denied HTTP response did not contain an event ID.")
    credentials = impersonated_credentials(config)
    credentials.refresh(Request())
    client = storage.Client(project=config["projectId"], credentials=credentials)
    text, entries = wait_for_audit(client.bucket(config["bucketName"]), config["auditObject"], expected)
    by_id = {entry["event_id"]: entry for entry in entries}
    for case in results:
        if case["eventId"] in expected:
            record = by_id[case["eventId"]]
            if record["country"].casefold() not in FORBIDDEN_COUNTRIES or record["status"] != 400:
                raise RuntimeError("The audit entry is not a valid country denial.")
            if case.get("country") and case["country"].casefold() != record["country"].casefold():
                raise RuntimeError("The persisted country differs from the curl request.")
            if case.get("filename") and case["filename"] != record["filename"]:
                raise RuntimeError("The persisted filename differs from the client request.")
            case["country"] = record["country"]
    args.results.with_name("forbidden-requests.jsonl").write_text(text, encoding="utf-8")
    checks = dict(complete=True, denied_events_verified=len(expected), total_audit_entries=len(entries),
                  audit_uri=f"gs://{config['bucketName']}/{config['auditObject']}")
    args.results.with_name("audit-verification.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(f"Verified {len(expected)} denied events, each persisted once in {checks['audit_uri']}.")
    if args.client_output:
        checks.update(requests=len(results), successful_downloads=sum(case["actual"] == 200 for case in results))
        args.results.write_text(json.dumps(results, indent=2), encoding="utf-8")
        args.results.with_name("verification.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
        print(f"Verified {checks['requests']} client responses: {checks['successful_downloads']} downloads and {len(expected)} country denials.")


if __name__ == "__main__":
    main()
