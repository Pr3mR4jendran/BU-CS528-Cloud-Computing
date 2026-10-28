"""Prepare/launch an unattended cloud experiment, inspect it, or collect exports."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

from experiment_support import now, run_name, save_json
from monitor_control import FUNCTION, MARKER, schedule
from provision import (ROOT, OWNER, PROJECT_ID, Gcloud, account, load_config, names, owned, remove_binding)

STOP_ROLE = "hw5ExperimentStop"
STOP_PERMISSIONS = "compute.instances.get,compute.instances.stop,cloudscheduler.jobs.get,cloudscheduler.jobs.enable"


def configure_access(gc, config):
    role = gc.describe(["iam", "roles", "describe", STOP_ROLE])
    if role is not None and role.get("description") != MARKER:
        raise RuntimeError("Experiment role exists without the HW5 ownership marker.")
    if role is not None and role.get("deleted"):
        gc.run(["iam", "roles", "undelete", STOP_ROLE])
    gc.run(["iam", "roles", "update" if role else "create", STOP_ROLE,
            "--title=HW5 experiment shutdown", f"--description={MARKER}", f"--permissions={STOP_PERMISSIONS}", "--stage=GA"])
    for permission in ("roles/cloudsql.client", f"projects/{PROJECT_ID}/roles/{STOP_ROLE}"):
        gc.run(["projects", "add-iam-policy-binding", PROJECT_ID, f"--member=serviceAccount:{account('client')}",
                f"--role={permission}", "--condition=None"])
    gc.run(["secrets", "add-iam-policy-binding", names(config)["secret"], f"--member=serviceAccount:{account('client')}",
            "--role=roles/secretmanager.secretAccessor", "--condition=None"])
    condition = f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/hw5-logs/')"
    gc.run(["storage", "buckets", "add-iam-policy-binding", "gs://" + config["bucketName"],
            f"--member=serviceAccount:{account('client')}", "--role=roles/storage.objectViewer",
            f"--condition=title={OWNER}-client-audit,expression={condition}"])
    function = gc.run(["functions", "describe", FUNCTION, "--gen2", f"--region={config['region']}"])
    owned(function)
    if function["state"] != "ACTIVE":
        raise RuntimeError("The SQL shutdown function must be ACTIVE before an unattended experiment.")
    service = function["serviceConfig"]["service"].rsplit("/", 1)[-1]
    gc.run(["run", "services", "add-iam-policy-binding", service, f"--region={config['region']}",
            f"--member=serviceAccount:{account('client')}", "--role=roles/run.invoker"])
    save_json(ROOT / ".state" / "experiment-access.json", dict(configured=True, role=STOP_ROLE))
    return function["serviceConfig"]["uri"]


def cleanup_access(gc, config):
    for role in ("roles/cloudsql.client", f"projects/{PROJECT_ID}/roles/{STOP_ROLE}"):
        remove_binding(gc, ["projects"], PROJECT_ID, account("client"), role)
    expression = f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/hw5-logs/')"
    remove_binding(gc, ["storage", "buckets"], "gs://" + config["bucketName"], account("client"),
        "roles/storage.objectViewer", dict(title=OWNER + "-client-audit", expression=expression))
    role = gc.describe(["iam", "roles", "describe", STOP_ROLE])
    if role is not None and not role.get("deleted"):
        if role.get("description") != MARKER:
            raise RuntimeError("Refusing to delete an unowned experiment role.")
        gc.run(["iam", "roles", "delete", STOP_ROLE])


def launch(gc, config, *, pilot=200, count=50000, seed=528):
    from google.cloud import storage
    from credentials import GcloudCredentials
    client = storage.Client(project=PROJECT_ID, credentials=GcloudCredentials())
    bucket = client.bucket(names(config)["artifact_bucket"])
    previous_vm = gc.run(["compute", "instances", "describe", OWNER + "-client", f"--zone={config['zone']}"])
    owned(previous_vm)
    attributes = {item["key"]: item["value"] for item in previous_vm.get("metadata", {}).get("items", [])}
    if attributes.get("hw5-experiment-job"):
        previous = json.loads(attributes["hw5-experiment-job"])
        progress = bucket.blob(f"experiments/{previous['jobId']}/progress.json")
        if not progress.exists() or json.loads(progress.download_as_text()).get("status") not in ("complete", "failed"):
            raise RuntimeError("A previous experiment has not completed. Inspect experiment.py status before launching another.")
    print("Checking that the complete configured file range exists...", flush=True)
    prefix = config["objectPrefix"].strip("/") + "/"
    objects = {blob.name for blob in client.list_blobs(config["bucketName"], prefix=prefix)}
    missing = [f"{prefix}{index}.html" for index in range(config["maxFileIndex"] + 1) if f"{prefix}{index}.html" not in objects]
    if missing:
        raise RuntimeError(f"Configured dataset is incomplete: {len(missing)} missing objects, including {missing[:5]}")
    job_id = run_name("exp-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    directory = ROOT / ".state" / "experiments" / job_id
    directory.mkdir(parents=True, exist_ok=False)
    save_json(directory / "dataset-verification.json", dict(expectedFiles=config["maxFileIndex"] + 1, missing=[], verifiedAt=now()))
    token = secrets.token_urlsafe(32)
    control = dict(peerIp=previous_vm["networkInterfaces"][0]["networkIP"], keyHash=hashlib.sha256(token.encode()).hexdigest())
    save_json(directory / "server-control.json", control)
    server = gc.run(["compute", "instances", "describe", OWNER + "-server", f"--zone={config['zone']}"])
    owned(server)
    old_release = {item["key"]: item["value"] for item in server.get("metadata", {}).get("items", [])}.get("hw5-sha256")
    schedule(gc, config, True)
    gc.run(["compute", "instances", "add-metadata", OWNER + "-server", f"--zone={config['zone']}",
        f"--metadata-from-file=hw5-experiment-control={directory / 'server-control.json'}"])
    # All VM software and units are installed by startup metadata.
    subprocess.run([sys.executable, str(ROOT / "provision.py"), "update"], check=True)
    monitor_uri = configure_access(gc, config)
    deployment = json.loads((ROOT / ".state" / "deployment.json").read_text())
    if old_release == deployment["bundleSha256"]:
        gc.run(["compute", "instances", "stop", OWNER + "-server", f"--zone={config['zone']}"])
        gc.run(["compute", "instances", "start", OWNER + "-server", f"--zone={config['zone']}"])
        from provision import wait_ready
        wait_ready(gc, config, deployment["serverUrl"])
    job = dict(jobId=job_id, projectId=PROJECT_ID, region=config["region"], zone=config["zone"],
        artifactBucket=names(config)["artifact_bucket"], serverIp=server["networkInterfaces"][0]["networkIP"],
        clientIp=control["peerIp"], port=config["port"], bucketName=config["bucketName"], objectPrefix=config["objectPrefix"],
        maxFileIndex=config["maxFileIndex"], pilotRequests=pilot, requestsPerClient=count, seed=seed,
        requestTimeout=30, maxSeconds=21600, monitorUri=monitor_uri, controlToken=token)
    save_json(directory / "job-private.json", job)
    gc.run(["compute", "firewall-rules", "update", OWNER + "-http", f"--source-ranges={control['peerIp']}/32"])
    gc.run(["compute", "instances", "add-metadata", OWNER + "-client", f"--zone={config['zone']}",
            f"--metadata-from-file=hw5-experiment-job={directory / 'job-private.json'}"])
    gc.run(["compute", "instances", "stop", OWNER + "-client", f"--zone={config['zone']}"])
    gc.run(["compute", "instances", "start", OWNER + "-client", f"--zone={config['zone']}"])
    save_json(ROOT / ".state" / "latest-experiment.json", dict(jobId=job_id, artifactBucket=job["artifactBucket"], launchedAt=now()))
    print(f"Launched {job_id}. The client VM runs the pilot, full workload, analysis/export and shutdown independently of this terminal.")


def inspect_or_collect(gc, *, collect=False):
    from google.cloud import storage
    from credentials import GcloudCredentials
    saved = json.loads((ROOT / ".state" / "latest-experiment.json").read_text())
    client = storage.Client(project=PROJECT_ID, credentials=GcloudCredentials())
    prefix = f"experiments/{saved['jobId']}/"
    bucket = client.bucket(saved["artifactBucket"])
    progress = bucket.blob(prefix + "progress.json")
    if progress.exists():
        print(progress.download_as_text())
    else:
        print("Client startup is still preparing the experiment; inspect hw5-clients.service if this persists.")
    if collect:
        destination = ROOT / "evidence" / saved["jobId"]
        for blob in client.list_blobs(bucket.name, prefix=prefix):
            relative = Path(blob.name[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Unexpected evidence object path.")
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(path), timeout=180)
        print(f"Collected available experiment evidence to {destination}")
    instances = gc.run(["compute", "instances", "list", f"--filter=labels.owner={OWNER}"])
    # Never print instance metadata: it contains the private experiment control key.
    print(json.dumps([dict(name=vm["name"], status=vm["status"]) for vm in instances], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("launch", "status", "collect"))
    parser.add_argument("--pilot", type=int, default=200)
    parser.add_argument("--requests", type=int, default=50000)
    parser.add_argument("--seed", type=int, default=528)
    args = parser.parse_args()
    if not 20 <= args.pilot <= 1000 or not 1 <= args.requests <= 50000:
        parser.error("Pilot must be 20-1000 per client; final requests must be 1-50000 per client.")
    gc = Gcloud()
    if args.action == "launch":
        launch(gc, load_config(ROOT / "config.json"), pilot=args.pilot, count=args.requests, seed=args.seed)
    else:
        inspect_or_collect(gc, collect=args.action == "collect")


if __name__ == "__main__":
    main()
