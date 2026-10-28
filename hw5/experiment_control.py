"""Control and snapshot support restricted to the configured experiment VM/key."""
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path

from experiment_support import run_name
from service import Result

CONTROL_PATH = "/__hw5/experiment"


class ExperimentControl:
    def __init__(self, config, storage_client, journal, failures, runtime_path):
        self.config, self.storage = config, storage_client
        self.journal, self.failures, self.runtime_path = journal, failures, Path(runtime_path)

    def handle(self, method, headers, body, peer, request_id):
        settings = self.config.get("experimentControl", {})
        headers = {key.casefold(): value for key, value in headers.items()}
        token = headers.get("authorization", "").removeprefix("Bearer ")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if (not token or peer != settings.get("peerIp") or not settings.get("keyHash")
                or not hmac.compare_digest(digest, settings["keyHash"])):
            return Result(403, b"Experiment control denied.\n", request_id=request_id)
        if method != "POST":
            return Result(405, b"POST required.\n", request_id=request_id)
        try:
            command = json.loads(body)
            action = command["action"]
            if action == "phase":
                run_id = run_name(command["runId"])
                ipaddress.ip_address(settings["peerIp"])
                self.config["runId"], self.config["workloadPeerIps"] = run_id, [settings["peerIp"]]
                # Keep a service restart within this boot in the same experiment.
                from experiment_support import save_json
                save_json(self.runtime_path, self.config)
                output = dict(runId=run_id)
            elif action == "export":
                run_id = run_name(self.config["runId"])
                bucket = self.storage.bucket(self.config["artifactBucket"])
                prefix = f"experiments/{run_id}/server"
                for name, journal in (("timings.jsonl", self.journal), ("database-failures.jsonl", self.failures)):
                    with journal.lock:
                        payload = journal.path.read_bytes() if journal.path.exists() else b""
                    bucket.blob(f"{prefix}/{name}").upload_from_string(payload, content_type="application/x-ndjson", timeout=120)
                output = dict(runId=run_id, bucket=bucket.name, prefix=prefix)
            else:
                raise ValueError("Unknown control action.")
        except (ValueError, KeyError, TypeError):
            return Result(400, b"Invalid experiment control request.\n", request_id=request_id)
        return Result(200, json.dumps(output).encode(), "application/json", request_id)
