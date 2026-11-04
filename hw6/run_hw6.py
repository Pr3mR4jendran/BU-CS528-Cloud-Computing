"""Run the HW6 models on a temporary Compute Engine VM."""

from base64 import b64encode
from contextlib import ExitStack, contextmanager
import csv
import ctypes
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tarfile
import tempfile
from time import monotonic, sleep

from sqlalchemy import text

from run_database import HW5, create_engine, local_session_lock, stop_instance, temporary_admin
from credentials import GcloudCredentials
from monitor_control import describe_job
from provision import Gcloud, account, load_config, owned, remove_binding, runtime_config
from sql_instance import resolved_config, set_activation_policy, validate_instance


ROOT = Path(__file__).resolve().parent
VM_LIFETIME_SECONDS = 1_080
VM_WAIT_SECONDS = 900
OUTPUT_FILES = ("country_predictions.csv", "income_predictions.csv", "metrics.json")
VM_FILES = ("train_models.py", "model_data.sql", "requirements.txt", "upload_results.py")


@contextmanager
def keep_awake():
    # Request that Windows stay awake while this thread supervises the cloud run.
    if os.name != "nt":
        yield
        return
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    if not set_state(0x80000001):
        raise RuntimeError("Windows could not set the system wake request.")
    try:
        yield
    finally:
        set_state(0x80000000)


def monitor_times(gc, config):
    job = describe_job(gc, config)
    if not job or job.get("state") != "ENABLED" or not job.get("scheduleTime"):
        raise RuntimeError("The HW5 shutdown monitor must be enabled and have a next run time.")
    owned(job)
    next_run = datetime.fromisoformat(job["scheduleTime"])
    last_run = datetime.fromisoformat(job["lastAttemptTime"]) if job.get("lastAttemptTime") else None
    return next_run, last_run


def wait_for_monitor_window(gc, config):
    deadline = monotonic() + 45 * 60
    announced = False
    while monotonic() < deadline:
        next_run, last_run = monitor_times(gc, config)
        now = datetime.now(timezone.utc)
        if (next_run - now).total_seconds() >= 28 * 60 and (
                last_run is None or (now - last_run).total_seconds() >= 90):
            return
        if not announced:
            print("Waiting for an open window between hourly HW5 shutdown checks. SQL is still stopped.",
                  flush=True)
            announced = True
        sleep(60)
    raise TimeoutError("No suitable window opened in the HW5 shutdown monitor schedule.")


def create_reader(gc, config, reader, password, cleanup):
    instance = config["sqlInstance"]
    cleanup.callback(gc.run, ["sql", "users", "delete", reader, f"--instance={instance}", "--host=%"],
                     missing_ok=True)
    with ExitStack() as admin_cleanup:
        admin, admin_password = temporary_admin(gc, instance, admin_cleanup)
        engine, connector = create_engine(runtime_config(config), credentials=GcloudCredentials(),
                                          user=admin, password=admin_password)
        admin_cleanup.callback(connector.close)
        admin_cleanup.callback(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text(f"CREATE USER '{reader}'@'%' IDENTIFIED BY :password"),
                               {"password": password})
            for table in ("requests", "client_ips", "request_country_observations"):
                connection.execute(text(f"GRANT SELECT ON hw6.{table} TO '{reader}'@'%'"))


def startup_payload(config, run_id):
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        for filename in VM_FILES:
            tar.add(ROOT / filename, arcname=filename)
        runtime = json.dumps(config).encode("utf-8")
        info = tarfile.TarInfo("runtime.json")
        info.size = len(runtime)
        tar.addfile(info, io.BytesIO(runtime))
    template = (ROOT / "startup.sh").read_text(encoding="utf-8")
    return template.replace("__HW6_RUN_ID__", run_id).replace(
        "__HW6_BUNDLE__", b64encode(bundle.getvalue()).decode("ascii")
    )


def delete_vm(gc, vm, zone):
    args = ["compute", "instances"]
    vm_info = gc.describe([*args, "describe", vm, f"--zone={zone}"])
    if vm_info:
        if vm_info.get("labels", {}).get("owner") != "cs528-hw6":
            raise RuntimeError(f"VM {vm} has an unexpected ownership label.")
        gc.run([*args, "delete", vm, f"--zone={zone}"], missing_ok=True)
    if gc.describe([*args, "describe", vm, f"--zone={zone}"]) is not None:
        raise RuntimeError(f"Deletion could not be verified for VM {vm}.")
    print(f"VM {vm} is absent.", flush=True)


def wait_for_results(gc, vm, zone, output_uri):
    print("Waiting for the VM to query Cloud SQL, fit both models, and upload the results.", flush=True)
    deadline = monotonic() + VM_WAIT_SECONDS
    serial = ""
    while monotonic() < deadline:
        if gc.describe(["storage", "objects", "describe", output_uri + "metrics.json"]) is not None:
            return
        output = gc.describe(["compute", "instances", "get-serial-port-output", vm, f"--zone={zone}"])
        if output is None:
            raise RuntimeError(f"VM {vm} disappeared before publishing metrics.")
        serial = output if isinstance(output, str) else output.get("contents", "")
        if "HW6_RUN_FAILED" in serial:
            raise RuntimeError("The VM startup script failed. Serial log tail:\n" + "\n".join(serial.splitlines()[-35:]))
        sleep(20)
    raise TimeoutError("The VM did not publish results in 15 minutes. Serial log tail:\n"
                       + "\n".join(serial.splitlines()[-35:]))


def print_results(gc, output_uri):
    with tempfile.TemporaryDirectory(prefix="hw6-results-") as directory:
        gc.run(["storage", "cp", *(output_uri + name for name in OUTPUT_FILES), directory])
        directory = Path(directory)
        metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
        files = {}
        for target in ("country", "income"):
            filename = f"{target}_predictions.csv"
            contents = (directory / filename).read_text(encoding="utf-8")
            records = list(csv.DictReader(io.StringIO(contents)))
            correct = sum(record[f"actual_{target}"] == record[f"predicted_{target}"] for record in records)
            test = metrics[target]["test"]
            if len(records) != test["rows"] or correct != test["correct"]:
                raise RuntimeError(f"Bucket predictions do not match the recorded {target} accuracy.")
            files[filename] = contents
        for filename, contents in files.items():
            print(f"\n{output_uri}{filename}\n{contents}", end="", flush=True)
        for target in ("country", "income"):
            test = metrics[target]["test"]
            print(f"{target.capitalize()} test accuracy {test['accuracy']:.2%} ({test['correct']:,}/{test['rows']:,}).")
        print(metrics["income"]["explanation"])
        print(f"Metrics and income evidence: {output_uri}metrics.json", flush=True)


def run(config):
    gc = Gcloud()
    instance, zone, project = config["sqlInstance"], config["zone"], config["projectId"]
    identity = account("server")
    member = f"--member=serviceAccount:{identity}"
    suffix = secrets.token_hex(6)
    vm = f"cs528-hw6-{suffix}"
    reader = f"hw6_reader_{suffix}"
    secret = f"{vm}-db"
    prefix = f"hw6/{vm}/"
    bucket_uri = f"gs://{config['bucketName']}"
    output_uri = f"{bucket_uri}/{prefix}"
    password = "Aa1_" + secrets.token_urlsafe(32)
    gc.redactions.append(password)

    with keep_awake(), local_session_lock(instance), ExitStack() as cleanup:
        current = gc.run(["sql", "instances", "describe", instance])
        validate_instance(current, config)
        if current["settings"]["activationPolicy"] != "NEVER":
            raise RuntimeError(f"Start this stage with Cloud SQL {instance} stopped.")
        owned(gc.run(["iam", "service-accounts", "describe", identity]))
        owned(gc.run(["compute", "networks", "subnets", "describe", config["prefix"],
                      f"--region={config['region']}"]))
        bucket = gc.run(["storage", "buckets", "describe", bucket_uri])
        if not bucket.get("uniform_bucket_level_access"):
            raise RuntimeError("The HW5 bucket requires uniform bucket-level access for the temporary write grant.")

        wait_for_monitor_window(gc, config)
        cleanup.callback(stop_instance, gc, instance)
        set_activation_policy(gc, instance, "ALWAYS")
        create_reader(gc, config, reader, password, cleanup)

        cleanup.callback(gc.run, ["secrets", "delete", secret], missing_ok=True)
        gc.run(["secrets", "create", secret, "--replication-policy=automatic", "--ttl=7200s", "--data-file=-"],
               data=password)
        gc.run(["secrets", "add-iam-policy-binding", secret, member,
                "--role=roles/secretmanager.secretAccessor", "--condition=None"])
        expiration = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        condition = {
            "title": vm,
            "expression": f"resource.name.startsWith('projects/_/buckets/{config['bucketName']}/objects/{prefix}') "
                          f"&& request.time < timestamp('{expiration}')",
        }
        role = "roles/storage.objectCreator"
        group = ["storage", "buckets"]
        cleanup.callback(remove_binding, gc, group, bucket_uri, identity, role, condition)
        gc.run([*group, "add-iam-policy-binding", bucket_uri, member, f"--role={role}",
                f"--condition=title={condition['title']},expression={condition['expression']}"])

        next_check, _ = monitor_times(gc, config)
        if (next_check - datetime.now(timezone.utc)).total_seconds() < VM_LIFETIME_SECONDS + 60:
            raise RuntimeError("SQL startup used the available window before the next HW5 shutdown check.")
        runtime = {
            "projectId": project,
            "bucketName": config["bucketName"],
            "outputPrefix": prefix,
            "database": {
                "instanceConnectionName": runtime_config(config)["database"]["instanceConnectionName"],
                "user": reader,
                "passwordSecret": f"projects/{project}/secrets/{secret}/versions/latest",
            },
        }
        print(f"HW6 run {vm}. Results will be written to {output_uri}", flush=True)
        cleanup.callback(delete_vm, gc, vm, zone)
        with tempfile.TemporaryDirectory(prefix="hw6-startup-") as directory:
            startup = Path(directory) / "startup.sh"
            startup.write_text(startup_payload(runtime, vm), encoding="utf-8", newline="\n")
            gc.run(["compute", "instances", "create", vm, f"--zone={zone}", "--machine-type=e2-small",
                    "--image-project=ubuntu-os-cloud", "--image-family=ubuntu-2404-lts-amd64",
                    "--boot-disk-size=10GB", f"--subnet={config['prefix']}", f"--service-account={identity}",
                    "--scopes=cloud-platform", "--labels=owner=cs528-hw6", "--no-restart-on-failure",
                    f"--max-run-duration={VM_LIFETIME_SECONDS}s", "--instance-termination-action=DELETE",
                    f"--metadata-from-file=startup-script={startup}"])
        wait_for_results(gc, vm, zone, output_uri)
    print_results(gc, output_uri)


if __name__ == "__main__":
    try:
        run(resolved_config(HW5, load_config(HW5 / "config.json")))
    except Exception as error:
        sys.exit(f"HW6 run failed: {error}")
