"""Run exactly 100 requests with the provided binary on the separate client VM."""
from collections import Counter
from datetime import datetime, timezone
import json
import re
import shlex
import ast
import time

from deploy import ROOT, Gcloud, ssh, collect
from service import FORBIDDEN_COUNTRIES


def client_responses(text):
    responses, current = [], None
    for line in text.splitlines():
        request = re.match(r"^Requesting\s+(\S+)\s+from\s+(\S+)\s+(\d+)", line)
        if request:
            current = dict(filename=request[1].lstrip("/"), status=None, event_id=None, bodyValid=False)
            responses.append(current)
        elif current is not None:
            status = re.match(r"^(\d{3})\s+", line)
            event = re.match(r"^x-request-id:\s*(\S+)", line, re.I)
            if status:
                current["status"] = int(status[1])
            elif event:
                current["event_id"] = event[1]
            elif line.startswith(("b'", 'b"')):
                try:
                    body = ast.literal_eval(line)
                    current["bodyValid"] = isinstance(body, bytes) and body.startswith(
                        b"<!DOCTYPE html>" if current["status"] == 200 else b"Permission denied.")
                except (ValueError, SyntaxError):
                    pass
    return responses


def main():
    state = json.loads((ROOT / "state.json").read_text())
    if state["status"] != "ready":
        raise SystemExit("Run deploy.py up successfully first.")
    config = state["config"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / "evidence" / ("client-100-" + stamp)
    folder.mkdir(parents=True)
    gc = Gcloud(config, folder)
    arguments = ["/opt/cs528-hw4/http-client", "-d", state["serverIp"], "-b", "none",
        "-w", config["objectPrefix"].strip("/"), "-n", "100", "-i", str(config["maxFileIndex"]),
        "-p", str(config["port"]), "-r", "0", "-t", "60", "-v"]
    remote = "/tmp/hw4-client-" + stamp
    script = folder / "run-client.sh"
    script.write_text("#!/bin/bash\n" + shlex.join(arguments) + " > " + remote + ".stdout.txt 2> " + remote + ".stderr.txt\n",
                      encoding="utf-8", newline="\n")
    (folder / "command.json").write_text(json.dumps(dict(vm=config["prefix"] + "-client", arguments=arguments, startedAt=stamp), indent=2), encoding="utf-8")
    gc.run(["compute", "scp", str(script), config["prefix"] + "-client:" + remote + ".sh",
            "--zone=" + config["zone"], "--tunnel-through-iap"])
    result = ssh(gc, config, "client", "bash " + remote + ".sh", check=False, timeout=900)
    for stream in ("stdout", "stderr"):
        gc.run(["compute", "scp", config["prefix"] + "-client:" + remote + "." + stream + ".txt",
                str(folder / ("client." + stream + ".txt")), "--zone=" + config["zone"], "--tunnel-through-iap"])
    text = (folder / "client.stdout.txt").read_text(encoding="utf-8", errors="replace")
    responses = client_responses(text)
    counts = Counter(str(response["status"]) for response in responses)
    verification = dict(expectedRequests=100, observedResponses=len(responses), statusCounts=dict(counts),
        clientExit=result.returncode, passed=result.returncode == 0 and len(responses) == 100 and
        all(r["status"] in (200, 400) and r["event_id"] and r["bodyValid"] for r in responses))
    expected_denials = {r["event_id"]: r for r in responses if r["status"] == 400 and r["event_id"]}
    deadline = time.monotonic() + 90
    while True:
        audit = gc.run(["storage", "cat", "gs://" + config["bucketName"] + "/" + state["runtime"]["auditObject"]], check=False)
        entries = [json.loads(line) for line in audit.stdout.splitlines() if line.strip()] if audit.returncode == 0 else []
        ids = Counter(entry.get("event_id") for entry in entries)
        by_id = {entry.get("event_id"): entry for entry in entries}
        denials_valid = all(ids[event_id] == 1 and by_id[event_id].get("country", "").casefold() in FORBIDDEN_COUNTRIES and
            by_id[event_id].get("filename") == response["filename"] and by_id[event_id].get("status") == 400
            for event_id, response in expected_denials.items())
        if denials_valid or time.monotonic() >= deadline:
            break
        time.sleep(3)
    verification.update(denialsVerified=len(expected_denials) if denials_valid else 0,
                        passed=verification["passed"] and denials_valid)
    (folder / "http-results.json").write_text(json.dumps(responses, indent=2), encoding="utf-8")
    (folder / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
    collect(state, folder / "cloud")
    print(json.dumps(verification, indent=2))
    print("Full client output and cloud evidence: " + str(folder))
    if not verification["passed"]:
        raise SystemExit("Client run failed verification. Review stdout/stderr; an exit code alone is not proof of success.")


if __name__ == "__main__":
    main()
