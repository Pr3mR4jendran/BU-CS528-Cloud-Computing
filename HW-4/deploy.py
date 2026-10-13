"""One-command HW4 deployment and ownership-checked cleanup using gcloud."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
import uuid

ROOT = Path(__file__).resolve().parent
APP_FILES = ("service.py", "server.py", "subscriber.py", "audit.py", "requirements.txt")


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate(config):
    for key in ("projectId", "bucketName", "zone", "region"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", config[key]):
            raise ValueError(f"Invalid {key}.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,15}", config["prefix"]):
        raise ValueError("prefix must be 3-16 lowercase letters/digits/hyphens, starting with a letter.")
    if not config["zone"].startswith(config["region"] + "-"):
        raise ValueError("zone must belong to region.")
    if not isinstance(config["port"], int) or not 1024 <= config["port"] <= 65535:
        raise ValueError("Use an unprivileged HTTP port from 1024 through 65535.")
    if not isinstance(config["maxFileIndex"], int) or config["maxFileIndex"] < 0:
        raise ValueError("maxFileIndex must be a nonnegative integer.")
    if not re.fullmatch(r"[a-zA-Z0-9_/-]+/", config["objectPrefix"]) or ".." in config["objectPrefix"]:
        raise ValueError("objectPrefix must be a safe dataset prefix ending in '/'.")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", config["account"]):
        raise ValueError("account must be your signed-in Google account email.")
    for source in config["httpSourceRanges"]:
        ipaddress.IPv4Network(source)
    if not config["httpSourceRanges"]:
        raise ValueError("At least one HTTP source range is required.")
    for key in ("serverMachine", "clientMachine", "reporterMachine"):
        if not re.fullmatch(r"[a-z0-9-]+", config[key]):
            raise ValueError(f"Invalid {key}.")
    for key in ("clientImageFamily", "clientImageProject"):
        if key in config and not re.fullmatch(r"[a-z0-9-]+", config[key]):
            raise ValueError(f"Invalid {key}.")


def fingerprint(config):
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for name in APP_FILES:
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def is_missing(stderr):
    if re.search(r"PERMISSION_DENIED|UNAUTHENTICATED|permission.*denied|reauthentication|unrecognized arguments|invalid choice|\b40[13]\b", stderr, re.I):
        return False
    return bool(re.search(r"\bNOT_FOUND\b|\bwas not found\b|\bdoes not exist\b|\bcould not be found\b|\bnot found\b|No URLs matched|matched no objects or files", stderr, re.I))


class Gcloud:
    def __init__(self, config, evidence):
        self.config, self.evidence = config, evidence
        self.executable = shutil.which("gcloud.cmd") or shutil.which("gcloud")
        if not self.executable:
            raise RuntimeError("Install Google Cloud CLI and put gcloud on PATH.")
        evidence.mkdir(parents=True, exist_ok=True)

    def run(self, arguments, *, check=True, timeout=600):
        command = [self.executable, *arguments, "--project=" + self.config["projectId"],
                   "--account=" + self.config["account"], "--quiet"]
        print("gcloud " + " ".join(arguments[:4]), flush=True)
        result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        record = dict(timestamp=now(), arguments=arguments, exitCode=result.returncode,
                      stdout=result.stdout, stderr=result.stderr)
        with (self.evidence / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result

    def describe(self, arguments):
        result = self.run([*arguments, "--format=json"], check=False)
        if result.returncode:
            # IAM can return PERMISSION_DENIED for a deleted account. Prove
            # absence with an authorized list rather than suppressing that error.
            if arguments[:3] == ["iam", "service-accounts", "describe"]:
                listed = self.run(["iam", "service-accounts", "list", "--filter=email=" + arguments[3], "--format=json"])
                if not json.loads(listed.stdout):
                    return None
            if is_missing(result.stderr):
                return None
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)


def owned(resource, remote, owner):
    if resource["marker"] == "description":
        return remote.get("description") == "Managed by CS528 HW4; owner=" + owner
    return remote.get("labels", {}).get("hw4_owner") == owner


def resources(config, owner, bootstrap):
    p, zone, region = config["prefix"], config["zone"], config["region"]
    marker = "Managed by CS528 HW4; owner=" + owner
    labels = "hw4_owner=" + owner + ",assignment=cs528-hw4"
    items = []

    def add(kind, name, describe, create, delete, marker_type="description"):
        items.append(dict(kind=kind, name=name, describe=describe, create=create, delete=delete, marker=marker_type))

    for role in ("server", "reporter"):
        name = p + "-" + role
        email = name + "@" + config["projectId"] + ".iam.gserviceaccount.com"
        add("service-account", email, ["iam", "service-accounts", "describe", email],
            ["iam", "service-accounts", "create", name, "--description=" + marker, "--display-name=HW4 " + role],
            ["iam", "service-accounts", "delete", email])
    add("network", p + "-net", ["compute", "networks", "describe", p + "-net"],
        ["compute", "networks", "create", p + "-net", "--subnet-mode=custom", "--description=" + marker],
        ["compute", "networks", "delete", p + "-net"])
    add("subnet", p + "-subnet", ["compute", "networks", "subnets", "describe", p + "-subnet", "--region=" + region],
        ["compute", "networks", "subnets", "create", p + "-subnet", "--network=" + p + "-net",
         "--region=" + region, "--range=10.54.0.0/24", "--description=" + marker],
        ["compute", "networks", "subnets", "delete", p + "-subnet", "--region=" + region])
    for name, rules, ranges, tags in (
        (p + "-http", "tcp:" + str(config["port"]), ",".join(config["httpSourceRanges"]), p + "-server"),
        (p + "-ssh", "tcp:22", "35.235.240.0/20", p + "-ssh"),
    ):
        add("firewall", name, ["compute", "firewall-rules", "describe", name],
            ["compute", "firewall-rules", "create", name, "--network=" + p + "-net", "--allow=" + rules,
             "--source-ranges=" + ranges, "--target-tags=" + tags, "--description=" + marker],
            ["compute", "firewall-rules", "delete", name])
    add("address", p + "-ip", ["compute", "addresses", "describe", p + "-ip", "--region=" + region],
        ["compute", "addresses", "create", p + "-ip", "--region=" + region, "--description=" + marker],
        ["compute", "addresses", "delete", p + "-ip", "--region=" + region])
    topic, subscription = p + "-forbidden", p + "-reporter"
    add("topic", topic, ["pubsub", "topics", "describe", topic],
        ["pubsub", "topics", "create", topic, "--labels=" + labels],
        ["pubsub", "topics", "delete", topic], "labels")
    add("subscription", subscription, ["pubsub", "subscriptions", "describe", subscription],
        ["pubsub", "subscriptions", "create", subscription, "--topic=" + topic, "--ack-deadline=60",
         "--message-retention-duration=1d", "--expiration-period=never", "--labels=" + labels],
        ["pubsub", "subscriptions", "delete", subscription], "labels")
    for role in ("server", "reporter", "client"):
        name = p + "-" + role
        family = config.get("clientImageFamily", "ubuntu-2404-lts-amd64") if role == "client" else "debian-12"
        image_project = config.get("clientImageProject", "ubuntu-os-cloud") if role == "client" else "debian-cloud"
        add("disk", name, ["compute", "disks", "describe", name, "--zone=" + zone],
            ["compute", "disks", "create", name, "--zone=" + zone, "--size=10GB", "--type=pd-standard",
             "--image-family=" + family, "--image-project=" + image_project, "--labels=" + labels],
            ["compute", "disks", "delete", name, "--zone=" + zone], "labels")
    for role in ("server", "reporter", "client"):
        name = p + "-" + role
        create = ["compute", "instances", "create", name, "--zone=" + zone,
            "--machine-type=" + config[role + "Machine"], "--subnet=" + p + "-subnet",
            "--disk=name=" + name + ",boot=yes,auto-delete=yes", "--labels=" + labels,
            "--tags=" + p + "-ssh" + ("," + p + "-server" if role == "server" else ""),
            "--metadata=enable-oslogin=FALSE", "--metadata-from-file=startup-script=" + str(bootstrap / (role + ".sh"))]
        if role == "client":
            create += ["--no-service-account", "--no-scopes"]
        else:
            create += ["--service-account=" + name + "@" + config["projectId"] + ".iam.gserviceaccount.com", "--scopes=cloud-platform"]
        if role == "server":
            create += ["--address=" + p + "-ip"]
        add("instance", name, ["compute", "instances", "describe", name, "--zone=" + zone], create,
            ["compute", "instances", "delete", name, "--zone=" + zone, "--delete-disks=boot"], "labels")
    return items


def runtime_config(config, owner):
    p, project = config["prefix"], config["projectId"]
    return dict(config, topicPath=f"projects/{project}/topics/{p}-forbidden",
        subscriptionPath=f"projects/{project}/subscriptions/{p}-reporter",
        auditObject=f"hw4-logs/{owner}/forbidden-requests.jsonl", logName=p + "-" + owner)


def bootstrap_scripts(config, owner, directory):
    directory.mkdir(parents=True, exist_ok=True)
    runtime = runtime_config(config, owner)
    for role in ("server", "reporter", "client"):
        lines = ["#!/bin/bash", "set -euo pipefail", "export DEBIAN_FRONTEND=noninteractive",
                 "exec > >(tee -a /var/log/cs528-hw4-startup.log) 2>&1",
                 "echo 'HW4 bootstrap starting'", "install -d -m 755 /opt/cs528-hw4"]
        files = ["service.py", "server.py", "requirements.txt"] if role == "server" else ["subscriber.py", "audit.py", "requirements.txt"]
        if role == "client":
            lines += ["apt-get update", "apt-get install -y libc6 zlib1g ca-certificates curl",
                      "touch /opt/cs528-hw4/client-ready",
                      "echo 'HW4 client VM ready; deployment driver installs the supplied binary'" ]
        else:
            lines += ["if [ ! -x /opt/cs528-hw4/.venv/bin/python ]; then",
                      "  apt-get update", "  apt-get install -y python3 python3-venv ca-certificates curl",
                      "  python3 -m venv /opt/cs528-hw4/.venv", "fi"]
            for name in files:
                encoded = base64.b64encode((ROOT / name).read_bytes()).decode()
                lines += [f"printf '%s' '{encoded}' | base64 -d > /opt/cs528-hw4/{name}"]
            encoded = base64.b64encode(json.dumps(runtime).encode()).decode()
            lines += [f"printf '%s' '{encoded}' | base64 -d > /opt/cs528-hw4/runtime.json",
                      "if ! id -u hw4app >/dev/null 2>&1; then useradd --system --no-create-home --shell /usr/sbin/nologin hw4app; fi",
                      "/opt/cs528-hw4/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/cs528-hw4/requirements.txt"]
            program = "server.py" if role == "server" else "subscriber.py"
            unit = f"""[Unit]
Description=CS528 HW4 {role}
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
User=hw4app
WorkingDirectory=/opt/cs528-hw4
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/cs528-hw4/.venv/bin/python /opt/cs528-hw4/{program}
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
[Install]
WantedBy=multi-user.target
"""
            lines += ["cat > /etc/systemd/system/cs528-hw4-" + role + ".service <<'HW4_UNIT'", unit.rstrip(), "HW4_UNIT",
                      "systemctl daemon-reload", "systemctl enable --now cs528-hw4-" + role,
                      "echo 'HW4 bootstrap finished'"]
        (directory / (role + ".sh")).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def bindings(config, owner, directory):
    p, project = config["prefix"], config["projectId"]
    bucket = "gs://" + config["bucketName"]
    server = p + "-server@" + project + ".iam.gserviceaccount.com"
    reporter = p + "-reporter@" + project + ".iam.gserviceaccount.com"
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for member, role, scope, prefix, title in (
        (server, "roles/storage.objectViewer", ["storage", "buckets"], config["objectPrefix"], "hw4_dataset_" + owner),
        (reporter, "roles/storage.objectUser", ["storage", "buckets"], "hw4-logs/" + owner + "/", "hw4_audit_" + owner),
    ):
        condition = dict(title=title, description="HW4 owned run " + owner,
            expression=f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/{prefix}')")
        path = directory / (title + ".json")
        save(path, condition)
        flags = ["--member=serviceAccount:" + member, "--role=" + role, "--condition-from-file=" + str(path)]
        result.append(([*scope, "add-iam-policy-binding", bucket, *flags], [*scope, "remove-iam-policy-binding", bucket, *flags]))
    for scope, name, member, role in (
        (["pubsub", "topics"], p + "-forbidden", server, "roles/pubsub.publisher"),
        (["pubsub", "subscriptions"], p + "-reporter", reporter, "roles/pubsub.subscriber"),
        (["projects"], project, server, "roles/logging.logWriter"),
    ):
        flags = ["--member=serviceAccount:" + member, "--role=" + role]
        if scope == ["projects"]:
            flags += ["--condition=None"]
        result.append(([*scope, "add-iam-policy-binding", name, *flags], [*scope, "remove-iam-policy-binding", name, *flags]))
    for account in (server, reporter):
        flags = ["--member=user:" + config["account"], "--role=roles/iam.serviceAccountUser", "--condition=None"]
        result.append((["iam", "service-accounts", "add-iam-policy-binding", account, *flags],
                       ["iam", "service-accounts", "remove-iam-policy-binding", account, *flags]))
    return result


def ssh(gc, config, role, command, check=True, timeout=120):
    return gc.run(["compute", "ssh", config["prefix"] + "-" + role, "--zone=" + config["zone"],
                   "--tunnel-through-iap", "--command=" + command], check=check, timeout=timeout)


def wait_ready(gc, config, state):
    address = gc.describe(["compute", "addresses", "describe", config["prefix"] + "-ip", "--region=" + config["region"]])
    state["serverIp"] = address["address"]
    state["serverUrl"] = f"http://{state['serverIp']}:{config['port']}"
    save(ROOT / "state.json", state)
    deadline = time.monotonic() + 900
    print("Waiting up to 15 minutes for package installation and both services...", flush=True)
    while time.monotonic() < deadline:
        try:
            with urlopen(state["serverUrl"] + "/" + config["objectPrefix"] + "0.html", timeout=20) as response:
                if response.status != 200 or not response.read():
                    raise RuntimeError("Sample file was not served.")
            reporter = ssh(gc, config, "reporter", "sudo journalctl -u cs528-hw4-reporter --no-pager -n 100", check=False)
            if reporter.returncode == 0 and '"event_type": "subscriber_ready"' in reporter.stdout:
                return
        except (URLError, OSError, RuntimeError):
            pass
        time.sleep(10)
    raise RuntimeError("Readiness timed out. Inspect serial/startup output; use collect, then down if needed.")


def up(config, evidence):
    validate(config)
    binary = ROOT / "http-client"
    if not binary.is_file() or binary.read_bytes()[:5] != b"\x7fELF\x02":
        raise RuntimeError("Place the instructor's 64-bit Linux http-client binary beside deploy.py.")
    state_path = ROOT / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    digest = fingerprint(config)
    if state and state["status"] != "deleted":
        if state["fingerprint"] != digest:
            raise RuntimeError("An owned deployment exists with different config/code. Collect and down before replacing it.")
    else:
        state = dict(schema=1, owner="r" + uuid.uuid4().hex, config=config, fingerprint=digest,
                     createdAt=now(), status="provisioning", resources=[])
    gc = Gcloud(config, evidence)
    project = gc.describe(["projects", "describe", config["projectId"]])
    if not project or project.get("lifecycleState") != "ACTIVE":
        raise RuntimeError("Project must be ACTIVE.")
    billing = gc.describe(["billing", "projects", "describe", config["projectId"]])
    if not billing or not billing.get("billingEnabled"):
        raise RuntimeError("Project billing is not enabled.")
    gc.run(["services", "enable", "compute.googleapis.com", "pubsub.googleapis.com", "storage.googleapis.com",
            "logging.googleapis.com", "iam.googleapis.com", "iap.googleapis.com", "cloudresourcemanager.googleapis.com"])
    bucket = gc.describe(["storage", "buckets", "describe", "gs://" + config["bucketName"]])
    sample = gc.describe(["storage", "objects", "describe", "gs://" + config["bucketName"] + "/" + config["objectPrefix"] + "0.html"])
    if not bucket or not sample or int(sample.get("size", 0)) <= 0:
        raise RuntimeError("An existing bucket containing the HW2 dataset is required.")
    save(evidence / "preflight.json", dict(project=project, billing=billing, bucket=bucket, sample=sample))
    for machine in set(config[role + "Machine"] for role in ("server", "client", "reporter")):
        if gc.describe(["compute", "machine-types", "describe", machine, "--zone=" + config["zone"]]) is None:
            raise RuntimeError("Machine type is unavailable: " + machine)
    bootstrap = ROOT / "evidence" / ("bootstrap-" + state["owner"])
    bootstrap_scripts(config, state["owner"], bootstrap)
    planned = resources(config, state["owner"], bootstrap)
    # Check every name before creating any VM or other chargeable resource.
    for resource in planned:
        remote = gc.describe(resource["describe"])
        if remote and not owned(resource, remote, state["owner"]):
            raise RuntimeError("Refusing to adopt an existing resource: " + resource["name"])
    state["resources"] = planned
    state["runtime"] = runtime_config(config, state["owner"])
    save(state_path, state)  # Track intents BEFORE mutations, including partial successes.
    try:
        for resource in planned:
            if resource["kind"] == "instance":
                continue
            if gc.describe(resource["describe"]) is None:
                gc.run(resource["create"])
        for add, _ in bindings(config, state["owner"], bootstrap):
            gc.run(add)
        for resource in planned:
            if resource["kind"] == "instance" and gc.describe(resource["describe"]) is None:
                gc.run(resource["create"])
        wait_ready(gc, config, state)
        for attempt in range(12):
            probe = ssh(gc, config, "client", "test -f /opt/cs528-hw4/client-ready", check=False)
            if probe.returncode == 0:
                break
            time.sleep(5)
        else:
            raise RuntimeError("Client bootstrap did not finish. Collect startup diagnostics.")
        remote_binary = "/tmp/http-client-" + state["owner"]
        gc.run(["compute", "scp", str(binary), config["prefix"] + "-client:" + remote_binary,
                "--zone=" + config["zone"], "--tunnel-through-iap"])
        ssh(gc, config, "client", "sudo install -m 755 " + remote_binary + " /opt/cs528-hw4/http-client")
        help_result = ssh(gc, config, "client", "/opt/cs528-hw4/http-client -h", check=False)
        (evidence / "http-client-help.txt").write_text(help_result.stdout + help_result.stderr, encoding="utf-8")
        # -h may return a nonzero usage exit, but dependency failures must be visible.
        if re.search(r"not found|error while loading|cannot execute|permission denied", help_result.stdout + help_result.stderr, re.I):
            raise RuntimeError("Client could not execute. See http-client-help.txt.")
        state["status"], state["readyAt"] = "ready", now()
        save(state_path, state)
        collect(state, evidence)
        print("READY: " + state["serverUrl"] + "\nEvidence: " + str(evidence))
    except Exception:
        state["status"] = "incomplete"
        save(state_path, state)
        print("Partial deployment is recorded in state.json. Use collect for diagnostics or down for cleanup.")
        raise


def collect(state, evidence):
    config, owner = state["config"], state["owner"]
    gc = Gcloud(config, evidence)
    save(evidence / "deployment.json", state)
    snapshots = []
    for resource in state["resources"]:
        remote = gc.describe(resource["describe"])
        snapshots.append(dict(kind=resource["kind"], name=resource["name"], owned=bool(remote and owned(resource, remote, owner)), remote=remote))
        if remote and owned(resource, remote, owner) and resource["kind"] == "instance":
            serial = gc.run(["compute", "instances", "get-serial-port-output", resource["name"], "--zone=" + config["zone"], "--port=1"], check=False)
            (evidence / (resource["name"] + "-serial.txt")).write_text(serial.stdout + serial.stderr, encoding="utf-8")
            if resource["name"].endswith(("-server", "-reporter")):
                role = resource["name"].rsplit("-", 1)[1]
                output = ssh(gc, config, role, "sudo journalctl -u cs528-hw4-" + role + " --no-pager -n 500", check=False)
                (evidence / (role + "-journal.txt")).write_text(output.stdout + output.stderr, encoding="utf-8")
    save(evidence / "resources.json", snapshots)
    runtime = state["runtime"]
    # Stress workloads can generate more than 1,000 denials. Preserve their logs
    # before down deletes the run's custom log, including any later browser demos.
    log_limit = 100000
    logs = gc.run(["logging", "read", f'logName="projects/{config["projectId"]}/logs/{runtime["logName"]}"',
                   "--limit=" + str(log_limit), "--order=asc", "--format=json"])
    (evidence / "cloud-logs.json").write_text(logs.stdout, encoding="utf-8")
    if len(json.loads(logs.stdout)) >= log_limit:
        raise RuntimeError("Cloud log export reached its limit; export the remaining entries before cleanup.")
    audit = gc.run(["storage", "cat", "gs://" + config["bucketName"] + "/" + runtime["auditObject"]], check=False)
    if audit.returncode and not is_missing(audit.stderr):
        raise RuntimeError("Audit export failed: " + audit.stderr)
    (evidence / "forbidden-requests.jsonl").write_text(audit.stdout, encoding="utf-8")
    for scope, name, filename in (
        (["storage", "buckets"], "gs://" + config["bucketName"], "bucket-iam.json"),
        (["pubsub", "topics"], config["prefix"] + "-forbidden", "topic-iam.json"),
        (["pubsub", "subscriptions"], config["prefix"] + "-reporter", "subscription-iam.json"),
    ):
        result = gc.run([*scope, "get-iam-policy", name, "--format=json"], check=False)
        (evidence / filename).write_text(result.stdout or result.stderr, encoding="utf-8")
    print("Evidence saved to " + str(evidence))


def verify_deleted_resource(gc, resource, deletion_confirmed=False):
    """Allow IAM's list/describe views to converge after a successful deletion."""
    attempts = 4 if deletion_confirmed and resource['kind'] == 'service-account' else 1
    for attempt in range(attempts):
        try:
            remote = gc.describe(resource['describe'])
            if remote is None or attempt == attempts - 1:
                return remote
        except RuntimeError:
            if attempt == attempts - 1:
                raise
        # Absence still requires an authorized successful check. A lingering
        # account or permission error is never converted into a success.
        print('Waiting for IAM deletion verification: ' + resource['name'], flush=True)
        time.sleep(2 ** attempt)


def down(state, evidence):
    config, owner = state["config"], state["owner"]
    gc = Gcloud(config, evidence)
    failures = []
    deleted = set()
    # Guard identities before changing IAM, including a resource recreated by someone else.
    for resource in state["resources"]:
        remote = gc.describe(resource["describe"])
        if remote and not owned(resource, remote, owner):
            raise RuntimeError("Cleanup ownership mismatch; refusing deletion: " + resource["name"])
    try:
        collect(state, evidence / "before-cleanup")
        audit_exported = True
    except Exception as error:
        audit_exported = False
        print("Evidence collection failed; retaining audit data if it cannot be exported: " + str(error))
    state["status"] = "deleting"
    save(ROOT / "state.json", state)

    def remove(resource):
        try:
            remote = gc.describe(resource["describe"])
            if remote:
                if not owned(resource, remote, owner):
                    raise RuntimeError("Ownership changed: " + resource["name"])
                if resource["kind"] == "instance":
                    boots = [d for d in remote.get("disks", []) if d.get("boot")]
                    if len(boots) != 1 or boots[0].get("source", "").rsplit("/", 1)[-1] != resource["name"]:
                        raise RuntimeError("VM boot disk differs from its tracked disk; refusing automatic deletion.")
                    disk = gc.describe(["compute", "disks", "describe", resource["name"], "--zone=" + config["zone"]])
                    if not disk or disk.get("labels", {}).get("hw4_owner") != owner:
                        raise RuntimeError("VM boot disk ownership mismatch.")
                gc.run(resource["delete"])
                deleted.add(resource['name'])
        except Exception as error:
            failures.append(resource["name"] + ": " + str(error))

    for resource in reversed(state["resources"]):
        if resource["kind"] == "instance":
            remove(resource)
    runtime = state["runtime"]
    audit_uri = "gs://" + config["bucketName"] + "/" + runtime["auditObject"]
    if audit_exported:
        result = gc.run(["storage", "rm", "--all-versions", audit_uri], check=False)
        if result.returncode and not is_missing(result.stderr):
            failures.append("audit data: " + result.stderr)
    else:
        failures.append("Audit data retained because evidence export failed; rerun down after resolving access.")
    directory = ROOT / "evidence" / ("bootstrap-" + owner)
    for _, remove_binding in bindings(config, owner, directory):
        if remove_binding[:2] == ["iam", "service-accounts"] and gc.describe(
                ["iam", "service-accounts", "describe", remove_binding[3]]) is None:
            continue  # Deleted account's own IAM policy no longer exists.
        result = gc.run(remove_binding, check=False)
        if result.returncode and not is_missing(result.stderr):
            failures.append("IAM removal: " + result.stderr)
    for resource in reversed(state["resources"]):
        if resource["kind"] != "instance":
            remove(resource)
    log_result = gc.run(["logging", "logs", "delete", runtime["logName"]], check=False)
    if log_result.returncode and not is_missing(log_result.stderr):
        failures.append("Cloud log removal: " + log_result.stderr)
    remaining = []
    for resource in state["resources"]:
        try:
            if verify_deleted_resource(gc, resource, resource['name'] in deleted) is not None:
                remaining.append(resource["name"])
        except Exception as error:
            failures.append("Cannot verify absence of " + resource["name"] + ": " + str(error))
    audit_remaining = gc.describe(["storage", "objects", "describe", audit_uri])
    if audit_remaining:
        remaining.append(audit_uri)
    state["status"] = "cleanup_failed" if failures or remaining else "deleted"
    state["cleanupAt"] = now()
    save(ROOT / "state.json", state)
    save(evidence / "cleanup.json", dict(status=state["status"], remaining=remaining, failures=failures,
        existingDatasetBucketPreserved=config["bucketName"], verifiedAt=now()))
    if failures or remaining:
        raise RuntimeError("Cleanup is incomplete. Inspect cleanup.json and rerun down; do not remove state.json.")
    print("CLEANUP VERIFIED: HW4 resources removed; original bucket and local evidence preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "up", "collect", "status", "down"),
                        help="up creates/starts VMs; down exports evidence and automatically deletes HW4 resources; status verifies tracked resources")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    if args.action in ("plan", "up"):
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        validate(config)
        if args.action == "plan":
            print(json.dumps(dict(config=config, resourceNames=[r["name"] for r in
                resources(config, "preview", ROOT / "evidence" / "preview")],
                note="Local preview only. No cloud resources created. down preserves existing dataset/HW3 resources."), indent=2))
            return
    else:
        state = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
        config = state["config"]  # Cleanup always uses the deployed project's saved configuration.
        validate(config)
    evidence = ROOT / "evidence" / (args.action + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    if args.action == "up":
        up(config, evidence)
    elif args.action == "collect":
        collect(state, evidence)
    elif args.action == "down":
        down(state, evidence)
    else:
        gc = Gcloud(config, evidence)
        for resource in state["resources"]:
            remote = gc.describe(resource["describe"])
            print(resource["kind"], resource["name"], "absent" if remote is None else
                  "owned" if owned(resource, remote, state["owner"]) else "OWNERSHIP MISMATCH")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error)) from error
