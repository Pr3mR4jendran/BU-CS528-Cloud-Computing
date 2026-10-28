"""Cloud-resident pilot, two-client workload, reconciliation, export and shutdown."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
import traceback
import urllib.request

import google.auth
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage
from google.oauth2 import id_token
import sqlalchemy as sa

from analyze import analyze
from database import create_engine, requests, verify_schema
from experiment_support import CLIENT_HASH, PREFIX, PROJECT, now, public_job, run_name, save_json

ROOT = Path(__file__).resolve().parent


def client_command(binary, job, count, *, host=None, port=None):
    return [str(binary), "-d", host or job["serverIp"], "-p", str(port or job["port"]),
        "-b", job["bucketName"], "-w", job["objectPrefix"].strip("/"), "-i", str(job["maxFileIndex"]),
        "-n", str(count), "-r", str(job["seed"]), "-t", str(job["requestTimeout"])]


def seed_probe(binary, job, output):
    captured = []
    class Capture(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(dict(target=self.path, headers={key.lower(): value for key, value in self.headers.items()
                if key.lower().startswith("x-")}))
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"probe\n")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sequences = []
    try:
        for _ in range(2):
            captured.clear()
            command = client_command(binary, job, 20, host="127.0.0.1", port=server.server_port)
            run = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if run.returncode or len(captured) != 20:
                raise RuntimeError("The instructor client failed its local compatibility/seed probe.")
            sequences.append(list(captured))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    def stable(sequence):
        return [dict(target=row["target"], headers={key: value for key, value in row["headers"].items() if key != "x-time"}) for row in sequence]
    result = dict(seed=job["seed"], countPerLaunch=20, sequences=sequences,
        sameSequenceExcludingTime=stable(sequences[0]) == stable(sequences[1]),
        binaryModified=False, note="Both launches use the same supplied seed flag; observations are preserved without changing the binary.")
    save_json(output / "seed-probe.json", result)
    return result


def run_pair(binary, job, count, output, deadline, progress=None):
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(startedAt=now(), countPerClient=count, expectedRequests=2 * count, seed=job["seed"], clients=[])
    started = time.monotonic()
    processes = []
    with ExitStack() as files:
        try:
            for number in (1, 2):
                command = client_command(binary, job, count)
                stdout = files.enter_context((output / f"client-{number}.stdout.log").open("wb"))
                stderr = files.enter_context((output / f"client-{number}.stderr.log").open("wb"))
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                processes.append(process)
                manifest["clients"].append(dict(number=number, command=command, pid=process.pid, startedAt=now()))
            save_json(output / "clients.json", manifest)
            last_progress = 0
            while any(process.poll() is None for process in processes):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Client workload exceeded the configured time limit.")
                if progress and time.monotonic() - last_progress >= 30:
                    progress(time.monotonic() - started)
                    last_progress = time.monotonic()
                time.sleep(.5)
            for client, process in zip(manifest["clients"], processes):
                client.update(exitCode=process.returncode, finishedAt=now())
            manifest.update(finishedAt=now(), elapsedSeconds=time.monotonic() - started,
                            complete=all(process.returncode == 0 for process in processes))
            save_json(output / "clients.json", manifest)
            if not manifest["complete"]:
                raise RuntimeError("At least one instructor client exited unsuccessfully; inspect its logs.")
            return manifest
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


class Pipeline:
    def __init__(self, job, runtime, base=Path("/var/lib/hw5/experiments")):
        if job["projectId"] != PROJECT or job["artifactBucket"] != runtime["artifactBucket"]:
            raise ValueError("Unexpected project or evidence bucket.")
        self.job, self.runtime = job, runtime
        self.job_id = run_name(job["jobId"])
        self.output = base / self.job_id
        self.output.mkdir(parents=True, exist_ok=True)
        self.storage = storage.Client(project=PROJECT)
        self.bucket = self.storage.bucket(job["artifactBucket"])
        self.engine = self.connector = None
        self.deadline = time.monotonic() + job.get("maxSeconds", 21600)
        self.result = dict(jobId=self.job_id, startedAt=now(), status="starting", phases=[])

    def upload(self, directory, prefix):
        for path in directory.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                self.bucket.blob(f"{prefix}/{path.relative_to(directory).as_posix()}").upload_from_filename(str(path), timeout=180)

    def checkpoint(self, stage, **extra):
        self.result.update(status=stage, updatedAt=now(), **extra)
        save_json(self.output / "progress.json", self.result)
        self.bucket.blob(f"experiments/{self.job_id}/progress.json").upload_from_string(
            json.dumps(self.result, default=str), content_type="application/json", timeout=60)
        print(json.dumps(dict(stage=stage, **extra), default=str), flush=True)

    def control(self, action, **data):
        payload = json.dumps(dict(action=action, **data)).encode()
        req = urllib.request.Request(f"http://{self.job['serverIp']}:{self.job['port']}/__hw5/experiment",
            data=payload, headers={"Authorization": "Bearer " + self.job["controlToken"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as response:
            return json.load(response)

    def reporter_drained(self, records, output):
        expected = {row["request_id"] for row in records if row["is_banned"] and row["status_code"] == 400}
        deadline = min(self.deadline - 900, time.monotonic() + 3600)
        while True:
            blob = self.storage.bucket(self.job["bucketName"]).blob("hw5-logs/forbidden.jsonl")
            from google.api_core.exceptions import NotFound
            try:
                content = blob.download_as_bytes(timeout=60)
            except NotFound:
                content = b""
            events = [json.loads(line) for line in content.splitlines() if line.strip()]
            found = {row.get("event_id") for row in events}
            if expected <= found:
                (output / "forbidden-audit.jsonl").write_bytes(content)
                save_json(output / "reporter-verification.json", dict(expectedEvents=len(expected), missingEventIds=[], complete=True))
                return
            if time.monotonic() >= deadline:
                save_json(output / "reporter-verification.json", dict(expectedEvents=len(expected), missingEventIds=sorted(expected - found), complete=False))
                raise RuntimeError("Reporter did not persist all expected forbidden-request events before its deadline.")
            self.checkpoint("reporter-draining", missingAuditEvents=len(expected - found))
            time.sleep(15)

    def phase(self, label, count):
        run_id = run_name(label + "-" + self.job_id)
        output = self.output / label
        output.mkdir(exist_ok=True)
        self.control("phase", runId=run_id)
        def progress(elapsed):
            with self.engine.connect() as connection:
                received = connection.scalar(sa.select(sa.func.count()).select_from(requests).where(
                    requests.c.run_id == run_id, requests.c.traffic_category == "workload"))
            self.checkpoint(label + "-running", runId=run_id, receivedRequests=received,
                expectedRequests=count * 2, elapsedSeconds=elapsed,
                estimatedSecondsRemaining=(count * 2 - received) * elapsed / received if received else None)
        self.checkpoint(label + "-running", runId=run_id, expectedRequests=count * 2)
        manifest = run_pair(ROOT / "http-client", self.job, count, output,
            min(self.deadline - 1200, time.monotonic() + (900 if label == "pilot" else 21600)), progress)
        exported = self.control("export")
        self.bucket.blob(exported["prefix"] + "/timings.jsonl").download_to_filename(str(output / "timings.jsonl"), timeout=180)
        self.bucket.blob(exported["prefix"] + "/database-failures.jsonl").download_to_filename(str(output / "database-failures.jsonl"), timeout=60)
        report, records = analyze(self.engine, run_id, count * 2, output / "timings.jsonl", output, peer_ip=self.job["clientIp"])
        if not report["complete"]:
            self.upload(output, f"experiments/{self.job_id}/{label}")
            raise RuntimeError(f"{label}: SQL/timing reconciliation failed; the run is incomplete.")
        self.reporter_drained(records, output)
        self.upload(output, f"experiments/{self.job_id}/{label}")
        phase = dict(label=label, runId=run_id, requests=count * 2, elapsedSeconds=manifest["elapsedSeconds"],
            complete=True, successful=report["successful"], unsuccessful=report["unsuccessful"])
        self.result["phases"].append(phase)
        self.checkpoint(label + "-complete", phase=phase)
        return phase

    def shutdown(self):
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        result, errors = {}, []
        with AuthorizedSession(credentials) as session:
            for name in (PREFIX + "-server", PREFIX + "-reporter"):
                try:
                    self.stop_vm(session, name, wait=True)
                    result[name] = "TERMINATED"
                except Exception as error:
                    errors.append(str(error))
            try:
                job = f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{self.job['region']}/jobs/{PREFIX}-sql-stop-hourly"
                response = session.get(job, timeout=30)
                response.raise_for_status()
                if response.json()["state"] == "PAUSED":
                    response = session.post(job + ":resume", json={}, timeout=30)
                    response.raise_for_status()
                result["scheduler"] = "ENABLED"
            except Exception as error:
                errors.append(str(error))
        try:
            uri = self.job["monitorUri"]
            token = id_token.fetch_id_token(Request(), uri)
            req = urllib.request.Request(uri, data=b"{}", headers={"Authorization": "Bearer " + token,
                "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as response:
                result["sqlMonitor"] = json.load(response)
            if result["sqlMonitor"].get("activationPolicy") != "NEVER":
                raise RuntimeError("SQL shutdown was not confirmed.")
        except Exception as error:
            errors.append(str(error))
        result["errors"] = errors
        return result

    def stop_vm(self, session, name, *, wait):
        if name not in (PREFIX + "-server", PREFIX + "-reporter", PREFIX + "-client"):
            raise RuntimeError("Refusing to stop an unexpected VM.")
        url = f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{self.job['zone']}/instances/{name}"
        response = session.get(url, timeout=30)
        response.raise_for_status()
        vm = response.json()
        if vm.get("labels", {}).get("owner") != PREFIX:
            raise RuntimeError("Refusing to stop a VM without the HW5 owner label.")
        if vm["status"] == "TERMINATED":
            return
        response = session.post(url + "/stop", json={}, timeout=30)
        response.raise_for_status()
        if wait:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                if response.json()["status"] == "TERMINATED":
                    return
                time.sleep(5)
            raise TimeoutError("VM did not finish stopping: " + name)

    def run(self):
        complete = self.output / "completion.json"
        if complete.exists():
            print("This experiment already finished; no clients were relaunched.", flush=True)
            return
        # A reboot after an incomplete job must not silently duplicate its workload.
        save_json(self.output / "job-public.json", public_job(self.job))
        workload_ok = False
        try:
            with (self.output / "started.json").open("x", encoding="utf-8") as stream:
                json.dump(dict(startedAt=now()), stream)
            actual = hashlib.sha256((ROOT / "http-client").read_bytes()).hexdigest()
            if actual != CLIENT_HASH:
                raise RuntimeError("The supplied HW5 client checksum changed.")
            self.checkpoint("compatibility-probe")
            probe = seed_probe(ROOT / "http-client", self.job, self.output)
            self.result["sameSeedSequenceExcludingTime"] = probe["sameSequenceExcludingTime"]
            self.engine, self.connector = create_engine(self.runtime)
            verify_schema(self.engine)
            pilot = self.phase("pilot", self.job["pilotRequests"])
            estimate = pilot["elapsedSeconds"] * self.job["requestsPerClient"] / self.job["pilotRequests"]
            self.result["estimatedFullRunSeconds"] = estimate
            if estimate > self.deadline - time.monotonic() - 1200:
                raise RuntimeError("Pilot predicts that the full run exceeds the configured six-hour guard; tune the bottleneck before relaunching.")
            self.phase("full", self.job["requestsPerClient"])
            workload_ok = True
        except Exception as error:
            self.result.update(error=str(error), traceback=traceback.format_exc())
            save_json(self.output / "error.json", dict(error=str(error), traceback=traceback.format_exc()))
        finally:
            if self.engine is not None:
                self.engine.dispose()
            if self.connector is not None:
                self.connector.close()
            try:
                self.checkpoint("shutting-down", workloadComplete=workload_ok)
            except Exception as error:
                self.result["checkpointError"] = str(error)
            try:
                shutdown = self.shutdown()
            except Exception as error:
                shutdown = dict(errors=[str(error)])
            self.result.update(status="complete" if workload_ok and not shutdown["errors"] else "failed",
                shutdown=shutdown, finishedAt=now(), clientStop="Requested after the final evidence upload; verify VM state with experiment.py status.")
            save_json(complete, self.result)
            try:
                self.upload(self.output, f"experiments/{self.job_id}")
                self.bucket.blob(f"experiments/{self.job_id}/progress.json").upload_from_filename(str(complete), content_type="application/json", timeout=60)
            finally:
                credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                with AuthorizedSession(credentials) as session:
                    self.stop_vm(session, PREFIX + "-client", wait=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    job = json.loads(args.job.read_text())
    runtime = json.loads((ROOT / "runtime.json").read_text())
    Pipeline(job, runtime).run()


if __name__ == "__main__":
    main()
