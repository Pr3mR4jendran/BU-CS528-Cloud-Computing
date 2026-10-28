"""Create/reuse the small SQL instance and recover from confirmed stockouts."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import tempfile
import time

PROJECT_ID = "bu-cs528-prem-project"
CAPACITY_CODES = {"ERROR_STOCKOUT", "ERROR_GCE_STOCKOUT"}


def selection_path(root):
    return root / ".state" / "sql-instance.json"


def read_selection(root, config):
    path = selection_path(root)
    if not path.exists():
        return {"projectId": PROJECT_ID, "requestedName": config["sqlInstance"],
                "name": config["sqlInstance"], "attempts": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("projectId") != PROJECT_ID or config["sqlInstance"] not in (state.get("requestedName"), state.get("name")):
        raise RuntimeError("Saved SQL selection does not match this project/configuration.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", state.get("name", "")):
        raise RuntimeError("Saved SQL instance name is invalid.")
    return state


def resolved_config(root, config):
    """Runtime and stop/cleanup always target the instance selected by setup."""
    result = dict(config)
    result["sqlInstance"] = read_selection(root, config)["name"]
    return result


def save_selection(root, selection, name, **details):
    selection["name"] = name
    selection["attempts"].append(dict(name=name, timestamp=datetime.now(timezone.utc).isoformat(), **details))
    path = selection_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    temporary.replace(path)


def candidate_name(base, index):
    return base if index == 1 else f"{base[:56]}-{index}"


def validate_instance(instance, config):
    settings = instance.get("settings", {})
    expected = dict(edition="ENTERPRISE", tier=config["sqlTier"], availabilityType="ZONAL")
    if instance.get("databaseVersion") != "MYSQL_8_4" or instance.get("region") != config["region"]:
        raise RuntimeError("Existing SQL instance must be MySQL 8.4 in the configured region; it was left unchanged.")
    for key, value in expected.items():
        if settings.get(key) != value:
            raise RuntimeError(f"Existing SQL {key} is {settings.get(key)!r}; expected {value!r}. "
                               "The instance was left unchanged to preserve its data and configuration.")
    if not settings.get("ipConfiguration", {}).get("ipv4Enabled"):
        raise RuntimeError("Existing SQL instance needs a public IP for the configured connector.")
    stopped = instance.get("state") == "STOPPED" and settings.get("activationPolicy") == "NEVER"
    if instance.get("state") != "RUNNABLE" and not stopped:
        raise RuntimeError(f"SQL instance is {instance.get('state')}; it is not ready for schema setup.")


def creation_stockout(gc, name, instance):
    """Only a failed CREATE with no successful CREATE permits name fallback."""
    if not instance or instance.get("state") != "FAILED":
        return False
    operations = gc.run(["sql", "operations", "list", f"--instance={name}", "--limit=50"])
    creates = [op for op in operations if op.get("operationType") == "CREATE"]
    if any(op.get("status") == "DONE" and not op.get("error") for op in creates):
        return False
    return any(op.get("status") == "DONE" and any(
        error.get("code") in CAPACITY_CODES for error in op.get("error", {}).get("errors", [])) for op in creates)


def wait_for_creation(gc, name, instance):
    if instance and instance.get("state") in ("PENDING_CREATE", "MAINTENANCE"):
        operations = gc.run(["sql", "operations", "list", f"--instance={name}", "--filter=status!=DONE", "--limit=50"])
        pending = [operation["name"] for operation in operations if operation.get("status") != "DONE"]
        if pending:
            try:
                gc.run(["sql", "operations", "wait", *pending, "--timeout=1800"])
            except RuntimeError:
                current = gc.describe(["sql", "instances", "describe", name])
                if not current or current.get("state") != "FAILED":
                    raise
                return current
        return gc.describe(["sql", "instances", "describe", name])
    return instance


def set_activation_policy(gc, name, policy):
    """Serialize SQL operations and explicitly wait for start/stop to finish."""
    if policy not in ("ALWAYS", "NEVER"):
        raise ValueError("SQL activation policy must be ALWAYS or NEVER.")
    for attempt in range(3):
        operations = gc.run(["sql", "operations", "list", f"--instance={name}", "--filter=status!=DONE", "--limit=50"])
        pending = [operation["name"] for operation in operations if operation.get("status") != "DONE"]
        if pending:
            print(f"Waiting for {len(pending)} active SQL operation(s) before changing activation policy.", flush=True)
            gc.run(["sql", "operations", "wait", *pending, "--timeout=1800"])
        current = gc.run(["sql", "instances", "describe", name])
        if current.get("state") == "FAILED":
            if policy == "NEVER":
                return current
            raise RuntimeError(f"SQL {name} is FAILED and cannot be started automatically.")
        target_state = current.get("state") == "RUNNABLE" if policy == "ALWAYS" else current.get("state") in ("STOPPED", "RUNNABLE")
        if current["settings"]["activationPolicy"] == policy and target_state:
            return current
        try:
            operation = gc.run(["sql", "instances", "patch", name, f"--activation-policy={policy}", "--async"])
        except RuntimeError as error:
            if attempt < 2 and "another operation was already in progress" in str(error):
                time.sleep(2)
                continue
            raise
        if not isinstance(operation, dict) or not operation.get("name"):
            raise RuntimeError("SQL activation patch did not return an operation ID.")
        gc.run(["sql", "operations", "wait", operation["name"], "--timeout=1800"])
        current = gc.run(["sql", "instances", "describe", name])
        if current["settings"]["activationPolicy"] != policy:
            raise RuntimeError(f"SQL activation policy did not become {policy}.")
        return current
    raise RuntimeError("SQL remained busy after three activation-policy attempts.")


def create_instance(gc, name, zone, config):
    root_password = "Aa1_" + secrets.token_urlsafe(32)
    gc.redactions.append(root_password)
    with tempfile.TemporaryDirectory(prefix="hw5-sql-") as directory:
        flags = Path(directory) / "flags.json"
        flags.write_text(json.dumps({"--root-password": root_password}), encoding="utf-8")
        flags.chmod(0o600)
        # --zone implies its region. The CLI treats --region and --zone as exclusive.
        gc.run(["sql", "instances", "create", name, "--database-version=MYSQL_8_4",
            "--edition=enterprise", f"--tier={config['sqlTier']}", f"--zone={zone}",
            "--availability-type=zonal", "--storage-type=SSD", f"--storage-size={config['sqlStorageGb']}",
            "--no-storage-auto-increase", "--assign-ip", "--deletion-protection", f"--flags-file={flags}"])


def ensure_sql_instance(gc, config, root):
    state = read_selection(root, config)
    base = state["requestedName"]
    name = state["name"]
    # Resolve deterministic suffixes, including after copying the working folder.
    index = 1
    while candidate_name(base, index) != name:
        index += 1
        if index > 1000:
            raise RuntimeError("Saved SQL instance is outside the managed name sequence.")
    zone_index = 0
    name_conflicts = 0
    while zone_index < len(config["sqlZones"]):
        zone = config["sqlZones"][zone_index]
        while True:
            instance = wait_for_creation(gc, name, gc.describe(["sql", "instances", "describe", name]))
            if not instance:
                break
            if instance.get("state") != "FAILED":
                validate_instance(instance, config)
                save_selection(root, state, name, state=instance["state"], zone=instance.get("gceZone"), reused=True)
                return instance
            if not creation_stockout(gc, name, instance):
                raise RuntimeError(f"SQL {name} is FAILED without a confirmed initial capacity failure. "
                                   "It was preserved; automatic replacement could discard access to existing data.")
            save_selection(root, state, name, state="FAILED", zone=instance.get("gceZone"), preserved=True)
            print(f"Preserving failed creation {name}; selecting a fresh instance name.", flush=True)
            index += 1
            if index > 1000:
                raise RuntimeError("Too many prior failed SQL instance names.")
            name = candidate_name(base, index)
        save_selection(root, state, name, state="PENDING_CREATE", zone=zone)
        print(f"Creating SQL {name}: Enterprise, {config['sqlTier']}, {config['sqlStorageGb']} GB SSD, {zone}", flush=True)
        try:
            create_instance(gc, name, zone, config)
        except RuntimeError as error:
            instance = wait_for_creation(gc, name, gc.describe(["sql", "instances", "describe", name]))
            if instance and instance.get("state") == "RUNNABLE":
                validate_instance(instance, config)
                save_selection(root, state, name, state="RUNNABLE", zone=instance.get("gceZone"))
                return instance
            # A recently deleted name can remain unavailable. Only retry a name
            # collision when describe confirms no live instance at that name.
            text = str(error)
            name_unavailable = instance is None and not re.search(r"PERMISSION_DENIED|not authorized|reauth", text, re.I) and bool(
                re.search(r"already exists|already been used|name.*(?:reserved|in use|not available)|INSTANCE_ALREADY_EXISTS", text, re.I))
            if name_unavailable:
                save_selection(root, state, name, state="ABSENT", zone=zone, nameUnavailable=True)
                name_conflicts += 1
                if name_conflicts >= 10:
                    raise RuntimeError("Ten SQL names were unavailable; creation stopped without changing existing instances.") from error
                index += 1
                name = candidate_name(base, index)
                print("SQL name unavailable; trying a fresh name in the same zone.", flush=True)
                continue
            capacity = creation_stockout(gc, name, instance)
            # Some failed allocations leave no instance object behind.
            if instance is None:
                capacity = bool(re.search(r"ERROR_(?:GCE_)?STOCKOUT|zone or region does not have sufficient resources", str(error), re.I))
            save_selection(root, state, name, state=instance.get("state") if instance else "ABSENT",
                           zone=zone, capacityFailure=capacity)
            if not capacity:
                raise
            print(f"Capacity unavailable in {zone}; trying the next configured zone at the same size.", flush=True)
            index += 1
            name = candidate_name(base, index)
            zone_index += 1
            continue
        instance = gc.run(["sql", "instances", "describe", name])
        validate_instance(instance, config)
        save_selection(root, state, name, state="RUNNABLE", zone=instance.get("gceZone", zone))
        return instance
    raise RuntimeError("SQL capacity unavailable in all configured zones. Failed attempts were preserved. "
                       "Retry setup later; the script will use fresh names without deleting a database.")
