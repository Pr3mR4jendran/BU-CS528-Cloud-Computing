"""Automate HW5 infrastructure, updates, session lifecycle, and retained Cloud SQL."""
import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from sql_instance import ensure_sql_instance, resolved_config, set_activation_policy

ROOT = Path(__file__).resolve().parent
PROJECT_ID = "bu-cs528-prem-project"  # Required by the assignment; never read the active project.
OWNER = "cs528-hw5"
MARKER = "Managed by CS528 HW5 steps 1-3"
CLIENT_SHA256 = "1c4f429609445da8d31270c36f23732b2c81b449639b2efefb7a777f1213bcc6"
APP_FILES = ("server.py", "service.py", "subscriber.py", "audit.py", "request_data.py", "database.py", "metrics.py", "credentials.py", "requirements.txt", "startup.sh",
             "experiment_support.py", "experiment_control.py", "run_clients.py", "run_clients.sh", "analyze.py", "analyze_timings.py")


class Gcloud:
    def __init__(self):
        self.executable = shutil.which("gcloud")
        if not self.executable:
            raise RuntimeError("Install Google Cloud CLI first.")
        self.redactions = []

    def run(self, args, *, data=None, missing_ok=False):
        print("gcloud " + " ".join(args[:3]), flush=True)
        result = subprocess.run([self.executable, *args, f"--project={PROJECT_ID}", "--quiet", "--format=json"],
                                input=data, text=True, capture_output=True)
        if result.returncode:
            error = result.stderr
            for value in self.redactions:
                error = error.replace(value, "[REDACTED]")
            # Never interpret auth/permission failures as absent resources.
            missing = re.search(r"NOT_FOUND|was not found|does not exist|not found in|not found:\s*404|No URLs matched|HTTPError 404", error, re.I)
            denied = re.search(r"PERMISSION_DENIED|permission|authentication|reauth|credentials|forbidden", error, re.I)
            if missing_ok and missing and not denied:
                return None
            raise RuntimeError(f"gcloud {' '.join(args[:3])} failed:\n{error.strip()}")
        value = result.stdout.strip()
        if not value:
            return {}
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def describe(self, args):
        return self.run(args, missing_ok=True)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config.setdefault("sqlTier", "db-f1-micro")
    config.setdefault("sqlStorageGb", 10)
    config.setdefault("sqlZones", [f"us-central1-{letter}" for letter in "abcf"] if config["region"] == "us-central1" else [config["zone"]])
    if config["projectId"] != PROJECT_ID or config["prefix"] != OWNER:
        raise ValueError("Project and resource prefix must match this homework's hardcoded target.")
    for key in ("sqlInstance", "bucketName", "region", "zone"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", config[key]):
            raise ValueError(f"Invalid resource identifier: {key}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", config["databaseName"]) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", config["databaseUser"]):
        raise ValueError("Invalid database/user identifier.")
    if not 1 <= int(config["port"]) <= 65535 or not 1 <= len(config["runId"]) <= 64:
        raise ValueError("Invalid server port or run ID.")
    if not re.fullmatch(r"[A-Za-z0-9_/-]+", config["objectPrefix"]) or any(
            part in ("", ".", "..") for part in config["objectPrefix"].strip("/").split("/")):
        raise ValueError("Invalid dataset object prefix.")
    for cidr in config["httpSourceRanges"]:
        ipaddress.ip_network(cidr)
    for address in config["workloadPeerIps"]:
        ipaddress.ip_address(address)
    if config["sqlTier"] not in ("db-f1-micro", "db-g1-small"):
        raise ValueError("SQL tier must be a shared-core Enterprise tier: db-f1-micro or db-g1-small.")
    if type(config["sqlStorageGb"]) is not int or not 10 <= config["sqlStorageGb"] <= 100:
        raise ValueError("SQL storage must be between 10 and 100 GB.")
    if not isinstance(config["sqlZones"], list) or not 1 <= len(config["sqlZones"]) <= 4 or len(set(config["sqlZones"])) != len(config["sqlZones"]):
        raise ValueError("Provide between one and four distinct SQL zones.")
    if any(not re.fullmatch(re.escape(config["region"]) + r"-[a-z]", zone) for zone in config["sqlZones"]):
        raise ValueError("SQL fallback zones must belong to the configured region.")
    return config


def owned(resource):
    if resource.get("labels", {}).get("owner") != OWNER and resource.get("description") != MARKER:
        raise RuntimeError("Refusing to change a resource without this deployment's ownership marker.")


def ensure(gc, describe, create):
    existing = gc.describe(describe)
    if existing is None:
        gc.run(create)
        existing = gc.run(describe)
    owned(existing)
    return existing


def names(config):
    return dict(artifact_bucket=f"{OWNER}-artifacts-{PROJECT_ID}", network=OWNER,
                subnet=OWNER, address=f"{OWNER}-server-ip", topic=f"{OWNER}-denials",
                subscription=f"{OWNER}-denials-sub", secret=f"{OWNER}-db-password")


def account(role):
    return f"{OWNER}-{role}@{PROJECT_ID}.iam.gserviceaccount.com"


def decode_secret(value):
    """gcloud --format=json returns SecretVersion payload bytes as base64."""
    if isinstance(value, str) and value:
        return value
    try:
        decoded = base64.b64decode(value["payload"]["data"], validate=True).decode("utf-8")
    except (TypeError, KeyError, ValueError, binascii.Error, UnicodeError):
        raise RuntimeError("Secret Manager returned an invalid password payload.") from None
    if not decoded:
        raise RuntimeError("Secret Manager returned an empty password.")
    return decoded


def bootstrap_sql(gc, config, runtime):
    instance = config["sqlInstance"]
    db_name = config["databaseName"]
    if gc.describe(["sql", "databases", "describe", db_name, f"--instance={instance}"]) is None:
        gc.run(["sql", "databases", "create", db_name, f"--instance={instance}", "--charset=utf8mb4", "--collation=utf8mb4_unicode_ci"])
    secret = names(config)["secret"]
    ensure(gc, ["secrets", "describe", secret], ["secrets", "create", secret, "--replication-policy=automatic", f"--labels=owner={OWNER}"])
    versions = gc.run(["secrets", "versions", "list", secret, "--filter=state:ENABLED"])
    if versions:
        latest = max(versions, key=lambda version: int(version["name"].rsplit("/", 1)[-1]))
        value = gc.run(["secrets", "versions", "access", latest["name"].rsplit("/", 1)[-1], f"--secret={secret}"])
        password = decode_secret(value)
    else:
        password = "Aa1_" + secrets.token_urlsafe(32)
        gc.run(["secrets", "versions", "add", secret, "--data-file=-"], data=password)
    admin_password = "Aa1_" + secrets.token_urlsafe(32)
    gc.redactions.extend((password, admin_password))
    admin = "hw5_bootstrap"
    users = gc.run(["sql", "users", "list", f"--instance={instance}"])
    operation = "set-password" if any(u.get("name") == admin for u in users) else "create"
    # The temporary flags file avoids putting a password in command arguments.
    with tempfile.TemporaryDirectory(prefix="hw5-auth-") as directory:
        flags = Path(directory) / "flags.json"
        flags.write_text(json.dumps({"--password": admin_password, "--instance": instance, "--host": "%"}), encoding="utf-8")
        flags.chmod(0o600)
        gc.run(["sql", "users", operation, admin, f"--flags-file={flags}"])
    try:
        environment = dict(os.environ, HW5_ADMIN_PASSWORD=admin_password, HW5_APP_PASSWORD=password)
        result = subprocess.run([sys.executable, str(ROOT / "setup_schema.py"),
            "--config", str(ROOT / ".state" / "runtime.json"), "--gcloud-auth", "--admin-user", admin,
            "--grant-user", config["databaseUser"]], env=environment, text=True, capture_output=True)
        if result.returncode:
            output = result.stderr
            for value in gc.redactions:
                output = output.replace(value, "[REDACTED]")
            raise RuntimeError("Schema setup failed: " + output)
        print(result.stdout.strip(), flush=True)
    finally:
        gc.run(["sql", "users", "delete", admin, f"--instance={instance}", "--host=%"])


def runtime_config(config):
    n = names(config)
    return dict(projectId=PROJECT_ID, bucketName=config["bucketName"], objectPrefix=config["objectPrefix"],
        port=config["port"], topicPath=f"projects/{PROJECT_ID}/topics/{n['topic']}",
        subscriptionPath=f"projects/{PROJECT_ID}/subscriptions/{n['subscription']}",
        auditObject="hw5-logs/forbidden.jsonl", logName=f"{OWNER}-requests", runId=config["runId"],
        workloadPeerIps=config["workloadPeerIps"], failureJournal="/var/lib/hw5/database-failures.jsonl",
        timingJournal="/var/lib/hw5/timings.jsonl", artifactBucket=n["artifact_bucket"],
        database=dict(instanceConnectionName=f"{PROJECT_ID}:{config['region']}:{config['sqlInstance']}",
            name=config["databaseName"], user=config["databaseUser"],
            passwordSecret=f"projects/{PROJECT_ID}/secrets/{n['secret']}/versions/latest"))


def build_bundle(runtime, client):
    payload = client.read_bytes()
    if hashlib.sha256(payload).hexdigest() != CLIENT_SHA256:
        raise ValueError("Supply the new HW5 Linux http-client binary; its checksum does not match.")
    stream = io.BytesIO()
    # Stable member timestamps make repeat setup detect actual code/config changes.
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        members = [(name, (ROOT / name).read_bytes()) for name in APP_FILES]
        members += [("runtime.json", json.dumps(runtime, sort_keys=True).encode()), ("http-client", payload)]
        for name, contents in members:
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(contents), 0
            info.mode = 0o755 if name == "http-client" else 0o644
            archive.addfile(info, io.BytesIO(contents))
    # tarfile's gzip header includes wall time; hash the uncompressed archive instead
    # by rebuilding with an explicit gzip mtime.
    import gzip
    return gzip.compress(gzip.decompress(stream.getvalue()), mtime=0)


def setup(gc, config, client, *, sql_only=False, update=False):
    if not sql_only:
        if not client.is_file():
            raise ValueError("Place the supplied Linux http-client beside setup.sh, or pass --client PATH.")
        # Validate the bundle before starting any billable resources.
        build_bundle(runtime_config(config), client)
    gc.run(["auth", "print-access-token"])
    gc.run(["services", "enable", "compute.googleapis.com", "sqladmin.googleapis.com", "pubsub.googleapis.com",
            "storage.googleapis.com", "secretmanager.googleapis.com", "logging.googleapis.com"])
    gc.run(["storage", "buckets", "describe", "gs://" + config["bucketName"]])
    state_dir = ROOT / ".state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        instance = ensure_sql_instance(gc, config, ROOT)
        config = resolved_config(ROOT, config)
        runtime = runtime_config(config)
        (state_dir / "runtime.json").write_text(json.dumps(runtime, indent=2), encoding="utf-8")
        if instance["settings"]["activationPolicy"] != "ALWAYS":
            set_activation_policy(gc, config["sqlInstance"], "ALWAYS")
        bootstrap_sql(gc, config, runtime)
        if sql_only:
            print(json.dumps(dict(sqlInstance=config["sqlInstance"], state="RUNNABLE", schemaReady=True,
                                  instanceConnectionName=runtime["database"]["instanceConnectionName"]), indent=2))
            return
        bundle = build_bundle(runtime, client)
        n = names(config)
        region, zone = config["region"], config["zone"]
        artifact_uri = "gs://" + n["artifact_bucket"]
        if gc.describe(["storage", "buckets", "describe", artifact_uri]) is None:
            gc.run(["storage", "buckets", "create", artifact_uri, f"--location={region}",
                    "--uniform-bucket-level-access", "--public-access-prevention", "--soft-delete-duration=0"])
            gc.run(["storage", "buckets", "update", artifact_uri, f"--update-labels=owner={OWNER}"])
        owned(gc.run(["storage", "buckets", "describe", artifact_uri]))
        for role in ("server", "reporter", "client"):
            ensure(gc, ["iam", "service-accounts", "describe", account(role)],
                ["iam", "service-accounts", "create", f"{OWNER}-{role}", f"--description={MARKER}"])
            gc.run(["storage", "buckets", "add-iam-policy-binding", "gs://" + n["artifact_bucket"],
                    f"--member=serviceAccount:{account(role)}", "--role=roles/storage.objectViewer", "--condition=None"])
        for role in ("server", "client"):
            gc.run(["storage", "buckets", "add-iam-policy-binding", artifact_uri,
                f"--member=serviceAccount:{account(role)}", "--role=roles/storage.objectUser",
                f"--condition=title={OWNER}-experiment-{role},expression=resource.name.startsWith('projects/_/buckets/{n['artifact_bucket']}/objects/experiments/')"])
        for role in ("roles/cloudsql.client", "roles/logging.logWriter"):
            gc.run(["projects", "add-iam-policy-binding", PROJECT_ID, f"--member=serviceAccount:{account('server')}",
                    f"--role={role}", "--condition=None"])
        gc.run(["secrets", "add-iam-policy-binding", n["secret"], f"--member=serviceAccount:{account('server')}",
                "--role=roles/secretmanager.secretAccessor", "--condition=None"])
        ensure(gc, ["pubsub", "topics", "describe", n["topic"]],
            ["pubsub", "topics", "create", n["topic"], f"--labels=owner={OWNER}"])
        ensure(gc, ["pubsub", "subscriptions", "describe", n["subscription"]],
            ["pubsub", "subscriptions", "create", n["subscription"], f"--topic={n['topic']}", f"--labels=owner={OWNER}"])
        gc.run(["pubsub", "topics", "add-iam-policy-binding", n["topic"], f"--member=serviceAccount:{account('server')}",
                "--role=roles/pubsub.publisher"])
        gc.run(["pubsub", "subscriptions", "add-iam-policy-binding", n["subscription"],
                f"--member=serviceAccount:{account('reporter')}", "--role=roles/pubsub.subscriber"])
        for role, prefix, permission in (("server", config["objectPrefix"].strip("/") + "/", "roles/storage.objectViewer"),
                                        ("reporter", "hw5-logs/", "roles/storage.objectUser")):
            expression = f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/{prefix}')"
            gc.run(["storage", "buckets", "add-iam-policy-binding", "gs://" + config["bucketName"],
                f"--member=serviceAccount:{account(role)}", f"--role={permission}",
                f"--condition=title={OWNER}-{role},expression={expression}"])
        ensure(gc, ["compute", "networks", "describe", n["network"]],
            ["compute", "networks", "create", n["network"], "--subnet-mode=custom", f"--description={MARKER}"])
        ensure(gc, ["compute", "networks", "subnets", "describe", n["subnet"], f"--region={region}"],
            ["compute", "networks", "subnets", "create", n["subnet"], f"--region={region}",
             f"--network={n['network']}", "--range=10.28.0.0/24", f"--description={MARKER}"])
        for suffix, sources, rules, tags in (("http", ",".join(config["httpSourceRanges"]), f"tcp:{config['port']}", f"{OWNER}-server"),
                                             ("iap", "35.235.240.0/20", "tcp:22", OWNER)):
            firewall = ensure(gc, ["compute", "firewall-rules", "describe", f"{OWNER}-{suffix}"],
                ["compute", "firewall-rules", "create", f"{OWNER}-{suffix}", f"--network={n['network']}",
                 f"--source-ranges={sources}", f"--allow={rules}", f"--target-tags={tags}", f"--description={MARKER}"])
            if sorted(firewall.get("sourceRanges", [])) != sorted(sources.split(",")):
                gc.run(["compute", "firewall-rules", "update", f"{OWNER}-{suffix}", f"--source-ranges={sources}"])
        ip = ensure(gc, ["compute", "addresses", "describe", n["address"], f"--region={region}"],
            ["compute", "addresses", "create", n["address"], f"--region={region}", f"--description={MARKER}"])["address"]
        digest = hashlib.sha256(bundle).hexdigest()
        archive_path = state_dir / "bundle.tar.gz"
        archive_path.write_bytes(bundle)
        uri = f"gs://{n['artifact_bucket']}/releases/{digest}.tar.gz"
        gc.run(["storage", "cp", str(archive_path), uri])
        for role in ("server", "reporter", "client"):
            vm_name = f"{OWNER}-{role}"
            vm = gc.describe(["compute", "instances", "describe", vm_name, f"--zone={zone}"])
            if vm is not None:
                owned(vm)
                attrs = {i["key"]: i["value"] for i in vm.get("metadata", {}).get("items", [])}
                if attrs.get("hw5-sha256") != digest:
                    if not update:
                        raise RuntimeError("VM bundle differs. Run provision.py update to apply the new release through startup metadata.")
                    if vm["status"] == "RUNNING":
                        gc.run(["compute", "instances", "stop", vm_name, f"--zone={zone}"])
                    elif vm["status"] != "TERMINATED":
                        raise RuntimeError(f"VM {vm_name} is in transitional state {vm['status']}.")
                    gc.run(["compute", "instances", "add-metadata", vm_name, f"--zone={zone}",
                        f"--metadata=hw5-role={role},hw5-bundle={uri},hw5-sha256={digest}",
                        f"--metadata-from-file=startup-script={ROOT / 'startup.sh'}"])
                    gc.run(["compute", "instances", "start", vm_name, f"--zone={zone}"])
                    continue
                if vm["status"] == "TERMINATED":
                    gc.run(["compute", "instances", "start", vm_name, f"--zone={zone}"])
                elif vm["status"] != "RUNNING":
                    raise RuntimeError(f"VM {vm_name} is in transitional state {vm['status']}; retry after it settles.")
                continue
            args = ["compute", "instances", "create", vm_name, f"--zone={zone}", f"--machine-type={config[role+'Machine']}",
                "--image-family=ubuntu-2404-lts-amd64", "--image-project=ubuntu-os-cloud", "--boot-disk-size=10GB",
                f"--subnet={n['subnet']}", f"--service-account={account(role)}", "--scopes=cloud-platform",
                f"--tags={OWNER},{OWNER}-{role}", f"--labels=owner={OWNER}",
                f"--metadata=hw5-role={role},hw5-bundle={uri},hw5-sha256={digest}",
                f"--metadata-from-file=startup-script={ROOT / 'startup.sh'}"]
            if role == "server":
                args.append(f"--address={ip}")
            gc.run(args)
        deployment = dict(serverUrl=f"http://{ip}:{config['port']}", sqlInstance=config["sqlInstance"],
                          bundleSha256=digest, timingEnabled=True, createdAt=datetime.now(timezone.utc).isoformat())
        (state_dir / "deployment.json").write_text(json.dumps(deployment, indent=2), encoding="utf-8")
        print("Waiting for VM startup scripts and SQL-backed HTTP readiness...", flush=True)
        wait_ready(gc, config, deployment["serverUrl"])
        print(json.dumps(deployment, indent=2))
    except BaseException as error:
        detail = str(error)
        for value in gc.redactions:
            detail = detail.replace(value, "[REDACTED]")
        lines = detail.strip().splitlines()
        causes = [line for line in lines if "Error:" in line or "Exception:" in line or line.startswith("ERROR:")]
        print("Setup error: " + (causes[-1] if causes else lines[-1] if lines else type(error).__name__), file=sys.stderr)
        try:
            (state_dir / "setup-error.log").write_text(detail, encoding="utf-8")
        except OSError:
            pass
        print("Setup incomplete. Attempting to stop the selected SQL instance; use cleanup.sh to remove partial infrastructure.", file=sys.stderr)
        try:
            effective = resolved_config(ROOT, config)
            current = gc.describe(["sql", "instances", "describe", effective["sqlInstance"]])
            if (current and current.get("state") in ("RUNNABLE", "STOPPED", "MAINTENANCE", "PENDING_CREATE")
                    and current.get("settings", {}).get("edition") == "ENTERPRISE"
                    and current["settings"].get("tier") == config["sqlTier"]):
                set_activation_policy(gc, effective["sqlInstance"], "NEVER")
        except Exception as error:
            print(f"SQL stop failed: {error}", file=sys.stderr)
        raise


def wait_ready(gc, config, url):
    ready = set()
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        for role in ("server", "reporter", "client"):
            if role in ready:
                continue
            output = gc.run(["compute", "instances", "get-serial-port-output", f"{OWNER}-{role}", f"--zone={config['zone']}"])
            (ROOT / ".state" / f"{role}-serial.json").write_text(json.dumps(output), encoding="utf-8")
            expected = json.loads((ROOT / ".state" / "deployment.json").read_text())["bundleSha256"]
            if f"HW5_READY role={role} sha256={expected}" in str(output):
                ready.add(role)
                print(f"Startup complete: {role}", flush=True)
        if len(ready) == 3:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
                    if (response.status == 200 and response.read() == b"ready\n"
                            and response.headers.get("X-HW5-Bundle") == expected):
                        return
            except (OSError, TimeoutError):
                pass
        time.sleep(10)
    raise RuntimeError("Startup timed out. Inspect the saved VM serial output and firewall source ranges.")


def stop(gc, config, *, sql=True):
    config = resolved_config(ROOT, config)
    errors = []
    deployment_file = ROOT / ".state" / "deployment.json"
    if deployment_file.exists() and json.loads(deployment_file.read_text()).get("timingEnabled"):
        try:
            from export_evidence import export
            export(gc, config)
        except Exception as error:
            errors.append("Evidence export failed: " + str(error))
    for role in ("client", "server", "reporter"):
        try:
            vm = gc.describe(["compute", "instances", "describe", f"{OWNER}-{role}", f"--zone={config['zone']}"])
            if vm:
                owned(vm)
                if vm["status"] != "TERMINATED":
                    gc.run(["compute", "instances", "stop", f"{OWNER}-{role}", f"--zone={config['zone']}"])
        except Exception as error:
            errors.append(str(error))
    if sql:
        try:
            instance = gc.describe(["sql", "instances", "describe", config["sqlInstance"]])
            if instance:
                if instance.get("state") == "FAILED":
                    print(f"SQL {config['sqlInstance']} is FAILED; no running SQL instance to stop.", flush=True)
                else:
                    set_activation_policy(gc, config["sqlInstance"], "NEVER")
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("\n".join(errors))


def cleanup(gc, config):
    config = resolved_config(ROOT, config)
    n, zone, region = names(config), config["zone"], config["region"]
    evidence = ROOT / "evidence" / ("cleanup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    evidence.mkdir(parents=True, exist_ok=True)
    errors = []
    def attempt(action):
        try:
            return action()
        except Exception as error:
            errors.append(str(error))
    def export_audit():
        uri = f"gs://{config['bucketName']}/hw5-logs/forbidden.jsonl"
        if gc.describe(["storage", "objects", "describe", uri]) is not None:
            gc.run(["storage", "cp", uri, str(evidence / "forbidden.jsonl")])
            gc.run(["storage", "rm", uri])
    # Stop SQL even when evidence export or another teardown operation fails.
    attempt(lambda: stop(gc, config))
    if errors:
        (evidence / "cleanup.json").write_text(json.dumps(dict(cleanupAborted=True,
            resourcesRetained=True, errors=errors), indent=2), encoding="utf-8")
        raise RuntimeError("Cleanup stopped before deleting resources; resolve shutdown/evidence export errors and retry:\n" + "\n".join(errors))
    if (ROOT / ".state" / "monitor.json").exists():
        from monitor_control import cleanup as cleanup_monitor
        attempt(lambda: cleanup_monitor(gc, config))
    if (ROOT / ".state" / "experiment-access.json").exists():
        from experiment import cleanup_access
        attempt(lambda: cleanup_access(gc, config))
    for role in ("client", "server", "reporter"):
        vm_name = f"{OWNER}-{role}"
        def remove_vm(name=vm_name):
            vm = gc.describe(["compute", "instances", "describe", name, f"--zone={zone}"])
            if vm is not None:
                owned(vm)
                try:
                    output = gc.run(["compute", "instances", "get-serial-port-output", name, f"--zone={zone}"])
                    (evidence / f"{name}-serial.json").write_text(json.dumps(output), encoding="utf-8")
                except Exception as error:
                    errors.append(str(error))
                gc.run(["compute", "instances", "delete", name, f"--zone={zone}", "--delete-disks=boot"])
                if gc.describe(["compute", "instances", "describe", name, f"--zone={zone}"]) is not None:
                    raise RuntimeError(f"VM still exists after deletion: {name}")
        attempt(remove_vm)
    resources = [
        (["compute", "addresses", "describe", n["address"], f"--region={region}"], ["compute", "addresses", "delete", n["address"], f"--region={region}"]),
        (["pubsub", "subscriptions", "describe", n["subscription"]], ["pubsub", "subscriptions", "delete", n["subscription"]]),
        (["pubsub", "topics", "describe", n["topic"]], ["pubsub", "topics", "delete", n["topic"]]),
        (["secrets", "describe", n["secret"]], ["secrets", "delete", n["secret"]]),
    ]
    for suffix in ("http", "iap"):
        resources.append((["compute", "firewall-rules", "describe", f"{OWNER}-{suffix}"], ["compute", "firewall-rules", "delete", f"{OWNER}-{suffix}"]))
    resources.extend([
        (["compute", "networks", "subnets", "describe", n["subnet"], f"--region={region}"], ["compute", "networks", "subnets", "delete", n["subnet"], f"--region={region}"]),
        (["compute", "networks", "describe", n["network"]], ["compute", "networks", "delete", n["network"]]),
    ])
    def remove_resource(describe, delete):
        resource = gc.describe(describe)
        if resource is not None:
            owned(resource)
            gc.run(delete)
            if gc.describe(describe) is not None:
                raise RuntimeError("Resource still exists after deletion: " + " ".join(describe[:4]))
    for describe, delete in resources:
        attempt(lambda d=describe, r=delete: remove_resource(d, r))
    attempt(export_audit)
    for role, prefix, permission in (("server", config["objectPrefix"].strip("/") + "/", "roles/storage.objectViewer"),
                                    ("reporter", "hw5-logs/", "roles/storage.objectUser")):
        expression = f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/{prefix}')"
        attempt(lambda role=role, permission=permission, expression=expression: remove_binding(gc,
            ["storage", "buckets"], "gs://" + config["bucketName"], account(role), permission,
            {"title": f"{OWNER}-{role}", "expression": expression}))
    for role in ("roles/cloudsql.client", "roles/logging.logWriter"):
        attempt(lambda role=role: remove_binding(gc, ["projects"], PROJECT_ID, account("server"), role))
    for role in ("server", "reporter", "client"):
        attempt(lambda role=role: remove_resource(["iam", "service-accounts", "describe", account(role)],
                                                 ["iam", "service-accounts", "delete", account(role)]))
    def remove_bucket():
        uri = "gs://" + n["artifact_bucket"]
        bucket = gc.describe(["storage", "buckets", "describe", uri])
        if bucket is not None:
            owned(bucket)
            gc.run(["storage", "rm", "--recursive", uri])
            if gc.describe(["storage", "buckets", "describe", uri]) is not None:
                raise RuntimeError("Artifact bucket still exists after deletion.")
    attempt(remove_bucket)
    sql = attempt(lambda: gc.run(["sql", "instances", "describe", config["sqlInstance"]]))
    sql_not_running = bool(sql and (sql["settings"]["activationPolicy"] == "NEVER" or sql.get("state") == "FAILED"))
    if sql and not sql_not_running:
        errors.append("Cloud SQL is not stopped.")
    result = dict(sqlRetained=True, sqlStopped=sql_not_running, sqlInstance=config["sqlInstance"], errors=errors)
    (evidence / "cleanup.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if errors:
        raise RuntimeError("Cleanup incomplete; retry after fixing these errors:\n" + "\n".join(errors))
    print("Cleanup verified. SQL and its data are retained and stopped. Original dataset retained.")


def remove_binding(gc, group, resource, email, role, condition=None):
    identity = gc.describe(["iam", "service-accounts", "describe", email])
    if identity is not None:
        owned(identity)
    policy = gc.run([*group, "get-iam-policy", resource])
    expected = condition or {}
    for binding in policy.get("bindings", []):
        actual = binding.get("condition", {})
        if binding["role"] == role and f"serviceAccount:{email}" in binding["members"] and actual == expected:
            arg = "None" if not condition else f"title={condition['title']},expression={condition['expression']}"
            gc.run([*group, "remove-iam-policy-binding", resource, f"--member=serviceAccount:{email}", f"--role={role}", f"--condition={arg}"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "update", "stop", "cleanup", "export", "monitor-setup", "monitor-pause", "monitor-resume", "monitor-test"))
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--client", type=Path, default=ROOT / "http-client")
    parser.add_argument("--sql-only", action="store_true", help="Create/start SQL and initialize the schema without creating VMs.")
    args = parser.parse_args()
    if args.sql_only and args.action != "setup":
        parser.error("--sql-only applies only to setup")
    config = load_config(args.config)
    saved = ROOT / ".state" / "config.json"
    if saved.exists() and load_config(saved) != config:
        raise ValueError("Configuration changed. Run cleanup using the saved .state/config.json first.")
    gc = Gcloud()
    from monitor_control import ensure_deployed, schedule, test as test_monitor
    if args.action in ("setup", "update"):
        if (ROOT / ".state" / "monitor.json").exists():
            schedule(gc, config, True)
        setup(gc, config, args.client, sql_only=args.sql_only, update=args.action == "update")
        if not args.sql_only:
            ensure_deployed(gc, config)
    elif args.action == "stop":
        try:
            stop(gc, config)
        finally:
            if (ROOT / ".state" / "monitor.json").exists():
                schedule(gc, config, False)
        print("VMs and Cloud SQL stopped. Reserved IPs/storage can still incur charges; cleanup.sh releases the IP.")
    elif args.action == "cleanup":
        cleanup(gc, config)
    elif args.action == "monitor-setup":
        ensure_deployed(gc, config)
    elif args.action == "monitor-test":
        # End traffic before asking the function itself to stop SQL.
        stop(gc, config, sql=False)
        test_monitor(gc, config)
        schedule(gc, config, False)
    elif args.action == "export":
        from export_evidence import export
        export(gc, config)
    else:
        if not schedule(gc, config, args.action == "monitor-pause"):
            raise RuntimeError("No hourly monitor found; run provision.py monitor-setup first.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
