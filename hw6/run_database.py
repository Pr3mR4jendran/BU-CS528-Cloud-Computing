"""Run the HW6 source audit or migration from an authenticated gcloud session."""

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

import sqlalchemy as sa
from google.cloud import secretmanager
from google.cloud.sql.connector import Connector, IPTypes

from database_stage import inspect_source, migrate


ASSIGNMENTS = Path(__file__).resolve().parent.parent
HW5 = ASSIGNMENTS / "hw5"
if not (HW5 / "config.json").is_file():
    HW5 = ASSIGNMENTS / "HW-5" / "hw5"
QUERY_READ_TIMEOUT = 300
sys.path.insert(0, str(HW5))

from credentials import GcloudCredentials
from monitor_control import describe_job, schedule
from provision import Gcloud, load_config, runtime_config
from sql_instance import resolved_config, set_activation_policy, validate_instance


@contextmanager
def local_session_lock(instance):
    path = Path(tempfile.gettempdir()) / f"cs528-hw6-{instance}.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another HW6 database session is running on this machine.") from None
        yield


def create_engine(config, *, credentials, user=None, password=None):
    settings = config["database"]
    if password is None:
        password = os.environ.get("HW5_DB_PASSWORD")
    if password is None:
        with secretmanager.SecretManagerServiceClient(credentials=credentials) as client:
            password = client.access_secret_version(
                request={"name": settings["passwordSecret"]}
            ).payload.data.decode("utf-8")
    connector = Connector(ip_type=IPTypes.PUBLIC, credentials=credentials, refresh_strategy="LAZY")

    def connect():
        return connector.connect(
            settings["instanceConnectionName"], "pymysql", user=user or settings["user"],
            password=password, db=settings["name"], charset="utf8mb4", connect_timeout=15,
            read_timeout=QUERY_READ_TIMEOUT, write_timeout=30, init_command="SET time_zone = '+00:00'",
        )

    engine = sa.create_engine("mysql+pymysql://", creator=connect, pool_pre_ping=True, hide_parameters=True)
    return engine, connector


def temporary_admin(gc, instance, cleanup):
    username = "hw6_" + secrets.token_hex(10)
    password = "Aa1_" + secrets.token_urlsafe(32)
    gc.redactions.append(password)
    cleanup.callback(gc.run, ["sql", "users", "delete", username, f"--instance={instance}", "--host=%"],
                     missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="hw6-auth-") as directory:
        flags = Path(directory) / "flags.json"
        flags.write_text(json.dumps({
            "--instance": instance, "--host": "%", "--password": password,
        }), encoding="utf-8")
        gc.run(["sql", "users", "create", username, f"--flags-file={flags}"])
    return username, password


def stop_instance(gc, instance):
    stopped = set_activation_policy(gc, instance, "NEVER")
    if stopped["settings"]["activationPolicy"] != "NEVER":
        raise RuntimeError("Cloud SQL shutdown could not be verified.")
    print(f"Cloud SQL {instance}: activation policy NEVER ({stopped['state']}).", flush=True)


def restore_monitor(gc, config):
    if not schedule(gc, config, False) or (describe_job(gc, config) or {}).get("state") != "ENABLED":
        raise RuntimeError("The HW5 shutdown monitor could not be restored to ENABLED.")
    print("HW5 shutdown monitor: ENABLED.", flush=True)


def run_stage(mode, config):
    instance = config["sqlInstance"]
    gc = Gcloud()
    with local_session_lock(instance), ExitStack() as cleanup:
        current = gc.run(["sql", "instances", "describe", instance])
        validate_instance(current, config)
        if current["settings"]["activationPolicy"] != "NEVER":
            raise RuntimeError(f"Start this stage with Cloud SQL {instance} stopped.")
        job = describe_job(gc, config)
        if job and job["state"] == "ENABLED":
            cleanup.callback(restore_monitor, gc, config)
            if not schedule(gc, config, True):
                raise RuntimeError("The HW5 shutdown monitor disappeared before it could be paused.")
        cleanup.callback(stop_instance, gc, instance)
        set_activation_policy(gc, instance, "ALWAYS")

        options = {"credentials": GcloudCredentials()}
        if mode == "migrate":
            options["user"], options["password"] = temporary_admin(gc, instance, cleanup)
        engine, connector = create_engine(runtime_config(config), **options)
        cleanup.callback(connector.close)
        cleanup.callback(engine.dispose)
        if mode == "migrate":
            result = migrate(engine)
        else:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                result = inspect_source(connection)
        print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify", "migrate"))
    args = parser.parse_args()
    config = resolved_config(HW5, load_config(HW5 / "config.json"))
    if config["databaseName"] != "hw5":
        parser.error("The migration expects the original database to be named hw5.")
    try:
        run_stage(args.mode, config)
    except Exception as error:
        sys.exit(f"HW6 database stage failed: {error}")


if __name__ == "__main__":
    main()
