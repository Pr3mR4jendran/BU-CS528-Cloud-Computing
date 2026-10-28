"""Provision and coordinate an hourly, authenticated Cloud SQL shutdown function."""
import json
import hashlib
from datetime import datetime, timezone
import time

from provision import OWNER, PROJECT_ID, MARKER, ROOT, account, ensure, owned, remove_binding
from sql_instance import resolved_config

FUNCTION = OWNER + "-sql-stop"
JOB = OWNER + "-sql-stop-hourly"
ROLE = "hw5SqlStop"
PERMISSIONS = "cloudsql.instances.get,cloudsql.instances.update"
DEPLOYMENT_REVISION = "explicit-build-access-v2"


def build_source_condition(gc, config):
    project = gc.run(["projects", "describe", PROJECT_ID])
    number = str(project.get("projectNumber", ""))
    if not number.isdigit():
        raise RuntimeError("Could not determine the project number for function source permissions.")
    buckets = (f"gcf-v2-sources-{number}-{config['region']}",
               f"gcf-v2-uploads-{number}-{config['region']}.cloudfunctions.appspot.com",
               f"{OWNER}-function-source-{PROJECT_ID}")
    # Includes bucket listing and object reads in exactly these build-source buckets.
    # The conditional project grant also works before Functions creates its buckets.
    terms = []
    for bucket in buckets:
        resource = "projects/_/buckets/" + bucket
        terms.append(f"(resource.name == '{resource}' || resource.name.startsWith('{resource}/objects/'))")
    return dict(title=OWNER + "-build-sources", expression=" || ".join(terms))


def configure_build_access(gc, config, repository):
    condition = build_source_condition(gc, config)
    gc.run(["projects", "add-iam-policy-binding", PROJECT_ID, f"--member=serviceAccount:{account('build')}",
            "--role=roles/logging.logWriter", "--condition=None"])
    gc.run(["artifacts", "repositories", "add-iam-policy-binding", repository,
            f"--location={config['region']}", f"--member=serviceAccount:{account('build')}", "--role=roles/artifactregistry.writer"])
    gc.run(["projects", "add-iam-policy-binding", PROJECT_ID, f"--member=serviceAccount:{account('build')}",
            "--role=roles/storage.objectViewer",
            f"--condition=title={condition['title']},expression={condition['expression']}"])
    # Migrate the earlier deployment to Google's documented custom-build roles.
    remove_binding(gc, ["projects"], PROJECT_ID, account("build"), "roles/cloudbuild.builds.builder")
    return condition


def deploy_function(gc, args):
    for attempt in range(3):
        try:
            return gc.run(args)
        except RuntimeError as error:
            message = str(error).lower()
            propagation = "access to bucket" in message and "denied" in message
            if not propagation or attempt == 2:
                raise
            delay = 20 * (attempt + 1)
            print(f"Function source access has not propagated yet; retrying deployment in {delay}s.", flush=True)
            time.sleep(delay)


def ensure_deployed(gc, config):
    effective = resolved_config(ROOT, config)
    digest = hashlib.sha256(b"".join((ROOT / "monitor" / name).read_bytes() for name in ("main.py", "requirements.txt"))
                            + effective["sqlInstance"].encode() + DEPLOYMENT_REVISION.encode()).hexdigest()
    saved = ROOT / ".state" / "monitor.json"
    if saved.exists() and json.loads(saved.read_text()).get("sourceSha256") == digest:
        function = gc.describe(["functions", "describe", FUNCTION, "--gen2", f"--region={config['region']}"])
        if function is not None and function.get("state") == "ACTIVE" and describe_job(gc, config) is not None:
            owned(function)
            schedule(gc, config, True)
            return
    deploy(gc, config, digest)


def describe_job(gc, config):
    return gc.describe(["scheduler", "jobs", "describe", JOB, f"--location={config['region']}"])


def schedule(gc, config, paused):
    job = describe_job(gc, config)
    if job is None:
        return False
    owned(job)
    wanted = "PAUSED" if paused else "ENABLED"
    if job.get("state") != wanted:
        gc.run(["scheduler", "jobs", "pause" if paused else "resume", JOB, f"--location={config['region']}"])
    return True


def deploy(gc, config, digest=None):
    config = resolved_config(ROOT, config)
    region = config["region"]
    gc.run(["services", "enable", "cloudfunctions.googleapis.com", "run.googleapis.com",
            "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "cloudscheduler.googleapis.com"])
    (ROOT / ".state").mkdir(exist_ok=True)
    (ROOT / ".state" / "monitor.json").write_text(json.dumps(dict(deploymentPending=True)), encoding="utf-8")
    for role in ("monitor", "scheduler", "build"):
        ensure(gc, ["iam", "service-accounts", "describe", account(role)],
               ["iam", "service-accounts", "create", f"{OWNER}-{role}", f"--description={MARKER}"])
    existing = gc.describe(["iam", "roles", "describe", ROLE])
    if existing is not None and existing.get("description") != MARKER:
        raise RuntimeError("Custom monitor role exists without the HW5 ownership marker.")
    if existing is not None and existing.get("deleted"):
        gc.run(["iam", "roles", "undelete", ROLE])
    gc.run(["iam", "roles", "update" if existing else "create", ROLE,
            "--title=HW5 SQL shutdown", f"--description={MARKER}", f"--permissions={PERMISSIONS}", "--stage=GA"])
    gc.run(["projects", "add-iam-policy-binding", PROJECT_ID, f"--member=serviceAccount:{account('monitor')}",
            f"--role=projects/{PROJECT_ID}/roles/{ROLE}", "--condition=None"])
    repository = OWNER + "-functions"
    ensure(gc, ["artifacts", "repositories", "describe", repository, f"--location={region}"],
           ["artifacts", "repositories", "create", repository, f"--location={region}",
            "--repository-format=docker", f"--description={MARKER}", f"--labels=owner={OWNER}"])
    source_bucket = f"{OWNER}-function-source-{PROJECT_ID}"
    bucket = gc.describe(["storage", "buckets", "describe", "gs://" + source_bucket])
    if bucket is None:
        gc.run(["storage", "buckets", "create", "gs://" + source_bucket, f"--location={region}",
                "--uniform-bucket-level-access", "--public-access-prevention", "--soft-delete-duration=0"])
        gc.run(["storage", "buckets", "update", "gs://" + source_bucket, f"--update-labels=owner={OWNER}"])
    else:
        owned(bucket)
    build_condition = configure_build_access(gc, config, repository)
    function = gc.describe(["functions", "describe", FUNCTION, "--gen2", f"--region={region}"])
    if function is not None:
        owned(function)
    # An existing schedule stays paused throughout a redeploy and the working session.
    schedule(gc, config, True)
    deploy_function(gc, ["functions", "deploy", FUNCTION, "--gen2", f"--region={region}", "--runtime=python312",
            f"--source={ROOT / 'monitor'}", "--entry-point=stop_sql", "--trigger-http", "--no-allow-unauthenticated",
            f"--service-account={account('monitor')}", f"--build-service-account=projects/{PROJECT_ID}/serviceAccounts/{account('build')}",
            f"--stage-bucket={source_bucket}", f"--docker-repository=projects/{PROJECT_ID}/locations/{region}/repositories/{repository}",
            "--memory=256Mi", "--timeout=1800s", "--min-instances=0", "--max-instances=1",
            f"--update-env-vars=HW5_PROJECT={PROJECT_ID},HW5_SQL_INSTANCE={config['sqlInstance']}", f"--update-labels=owner={OWNER}"])
    function = gc.run(["functions", "describe", FUNCTION, "--gen2", f"--region={region}"])
    service = function["serviceConfig"]["service"].rsplit("/", 1)[-1]
    uri = function["serviceConfig"]["uri"]
    gc.run(["run", "services", "add-iam-policy-binding", service, f"--region={region}",
            f"--member=serviceAccount:{account('scheduler')}", "--role=roles/run.invoker"])
    job = describe_job(gc, config)
    if job is not None:
        owned(job)
    gc.run(["scheduler", "jobs", "update" if job else "create", "http", JOB, f"--location={region}",
            "--schedule=0 * * * *", "--time-zone=Etc/UTC", f"--uri={uri}", "--http-method=POST",
            f"--oidc-service-account-email={account('scheduler')}", f"--oidc-token-audience={uri}",
            "--attempt-deadline=1800s", "--max-retry-attempts=2", "--min-backoff=60s", "--max-backoff=300s", f"--description={MARKER}"])
    schedule(gc, config, True)
    result = dict(function=FUNCTION, uri=uri, schedulerJob=JOB, schedulerState="PAUSED", sqlInstance=config["sqlInstance"])
    result["sourceSha256"] = digest
    result["buildSourceCondition"] = build_condition
    (ROOT / ".state").mkdir(exist_ok=True)
    (ROOT / ".state" / "monitor.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def test(gc, config):
    """Invoke via the real Scheduler identity; wait for SQL and the job result."""
    config = resolved_config(ROOT, config)
    job = describe_job(gc, config)
    if job is None:
        raise RuntimeError("Deploy the monitor with provision.py monitor-setup first.")
    owned(job)
    # Scheduler's RunJob requires ENABLED, including for a manual test.
    schedule(gc, config, False)
    started = datetime.now(timezone.utc).isoformat()
    gc.run(["scheduler", "jobs", "run", JOB, f"--location={config['region']}"])
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        query = (f'resource.type="cloud_scheduler_job" AND resource.labels.job_id="{JOB}" '
                 f'AND resource.labels.location="{config["region"]}" AND timestamp>="{started}" '
                 'AND jsonPayload."@type"="type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished"')
        logs = gc.run(["logging", "read", query, "--limit=1", "--order=desc"])
        if logs:
            # Proto JSON can omit the default OK status on successful attempts.
            if (logs[0].get("jsonPayload", {}).get("status", "OK") != "OK"
                    or logs[0].get("severity") in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY")):
                raise RuntimeError("Scheduler invocation failed: " + json.dumps(logs[0]))
            sql = gc.run(["sql", "instances", "describe", config["sqlInstance"]])
            if sql["settings"]["activationPolicy"] == "NEVER":
                function_logs = gc.run(["logging", "read", f'resource.type="cloud_run_revision" AND '
                    f'resource.labels.service_name="{FUNCTION}" AND timestamp>="{started}" AND jsonPayload.event_type="sql_shutdown"',
                    "--limit=5", "--order=desc"])
                if not any(entry.get("jsonPayload", {}).get("action") in ("stopped", "already_stopped")
                           and entry["jsonPayload"].get("instance") == config["sqlInstance"]
                           and entry["jsonPayload"].get("activationPolicy") == "NEVER" for entry in function_logs):
                    time.sleep(20)  # Function logs can arrive after Scheduler logs.
                    continue
                result = dict(scheduler=logs[0], functionLogs=function_logs, sqlState=sql["state"], activationPolicy="NEVER")
                destination = ROOT / "evidence" / ("monitor-" + str(time.time_ns()) + ".json")
                destination.parent.mkdir(exist_ok=True)
                destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(f"Authenticated monitor invocation succeeded; SQL is stopped. Evidence: {destination}")
                return
        time.sleep(20)
    raise RuntimeError("Monitor test timed out. Inspect Scheduler/function logs before starting more work.")


def cleanup(gc, config):
    region = config["region"]
    job = describe_job(gc, config)
    if job is not None:
        owned(job)
        gc.run(["scheduler", "jobs", "delete", JOB, f"--location={region}"])
    function = gc.describe(["functions", "describe", FUNCTION, "--gen2", f"--region={region}"])
    if function is not None:
        owned(function)
        gc.run(["functions", "delete", FUNCTION, "--gen2", f"--region={region}"])
        # Delete this function's exact provider-managed source object, preserving
        # shared Cloud Functions source buckets and other coursework functions.
        source = function.get("buildConfig", {}).get("source", {}).get("storageSource", {})
        if source.get("bucket") and source.get("object"):
            if any(character in source["bucket"] + source["object"] for character in "*?[]#"):
                raise RuntimeError("Unexpected wildcard in the function's source reference.")
            uri = f"gs://{source['bucket']}/{source['object']}"
            if source.get("generation"):
                uri += "#" + str(source["generation"])
            if gc.describe(["storage", "objects", "describe", uri]) is not None:
                gc.run(["storage", "rm", uri])
    repository = OWNER + "-functions"
    artifact = gc.describe(["artifacts", "repositories", "describe", repository, f"--location={region}"])
    if artifact is not None:
        owned(artifact)
        gc.run(["artifacts", "repositories", "delete", repository, f"--location={region}"])
    source = "gs://" + f"{OWNER}-function-source-{PROJECT_ID}"
    bucket = gc.describe(["storage", "buckets", "describe", source])
    if bucket is not None:
        owned(bucket)
        gc.run(["storage", "rm", "--recursive", source])
    for name, role in (("monitor", f"projects/{PROJECT_ID}/roles/{ROLE}"), ("build", "roles/cloudbuild.builds.builder"),
                       ("build", "roles/logging.logWriter")):
        remove_binding(gc, ["projects"], PROJECT_ID, account(name), role)
    remove_binding(gc, ["projects"], PROJECT_ID, account("build"), "roles/storage.objectViewer", build_source_condition(gc, config))
    for name in ("monitor", "scheduler", "build"):
        identity = gc.describe(["iam", "service-accounts", "describe", account(name)])
        if identity is not None:
            owned(identity)
            gc.run(["iam", "service-accounts", "delete", account(name)])
    role = gc.describe(["iam", "roles", "describe", ROLE])
    if role is not None and not role.get("deleted"):
        if role.get("description") != MARKER:
            raise RuntimeError("Refusing to delete an unowned custom role.")
        gc.run(["iam", "roles", "delete", ROLE])
