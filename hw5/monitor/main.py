"""Authenticated HTTP function: stop only the configured Cloud SQL instance."""
import json
import os
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession


def stop_instance(session, project, instance, *, timeout=1500, clock=time.monotonic, sleep=time.sleep):
    base = f"https://sqladmin.googleapis.com/sql/v1beta4/projects/{project}"
    url = f"{base}/instances/{instance}"
    deadline = clock() + timeout

    def api(method, target, **kwargs):
        response = session.request(method, target, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    def wait(operation):
        while operation.get("status") != "DONE":
            if clock() >= deadline:
                raise TimeoutError("SQL operation remains pending; a retry will check it again.")
            sleep(5)
            operation = api("GET", f"{base}/operations/{operation['name']}")
        if operation.get("error"):
            raise RuntimeError("Cloud SQL operation failed: " + json.dumps(operation["error"]))

    # Avoid patching during a start/stop or maintenance operation. Follow pagination.
    token = None
    while True:
        params = dict(instance=instance, maxResults=100)
        if token:
            params["pageToken"] = token
        page = api("GET", f"{base}/operations", params=params)
        for operation in page.get("items", []):
            if operation.get("status") != "DONE":
                wait(operation)
        token = page.get("nextPageToken")
        if not token:
            break
    before = api("GET", url)
    if before.get("settings", {}).get("activationPolicy") == "NEVER":
        return dict(instance=instance, action="already_stopped", activationPolicy="NEVER", state=before["state"])
    if before.get("state") not in ("RUNNABLE", "STOPPED"):
        raise RuntimeError("Instance is not in a stoppable state: " + before.get("state", "unknown"))
    operation = api("PATCH", url, json={"settings": {"activationPolicy": "NEVER"}})
    operation_name = operation["name"]
    wait(operation)
    after = api("GET", url)
    if after.get("settings", {}).get("activationPolicy") != "NEVER":
        raise RuntimeError("SQL stop operation completed without the expected activation policy.")
    return dict(instance=instance, action="stopped", operation=operation_name,
                activationPolicy="NEVER", state=after["state"])


def stop_sql(request):
    if request.method != "POST":
        return ("POST required", 405, {"Allow": "POST"})
    # Body/query arguments never choose a project or an instance.
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        with AuthorizedSession(credentials) as session:
            result = stop_instance(session, os.environ["HW5_PROJECT"], os.environ["HW5_SQL_INSTANCE"])
        print(json.dumps(dict(severity="INFO", event_type="sql_shutdown", **result)), flush=True)
        return (json.dumps(result), 200, {"Content-Type": "application/json"})
    except Exception as error:
        print(json.dumps(dict(severity="ERROR", event_type="sql_shutdown_failed", error=str(error))), flush=True)
        return (json.dumps(dict(error="SQL stop failed; inspect function logs and retry.")), 503,
                {"Content-Type": "application/json"})
