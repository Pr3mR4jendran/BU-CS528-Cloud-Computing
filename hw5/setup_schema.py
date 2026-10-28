"""Create missing tables and grant only SELECT/INSERT to the application user."""
import argparse
import json
import os
from pathlib import Path
import re

import sqlalchemy as sa

from credentials import GcloudCredentials
from database import create_engine, identifier, metadata, verify_schema


def verify_application_grants(connection, database_name, app_user):
    """Do not revoke global grants: Cloud SQL protects its system schemas."""
    allowed = {"*.*": {"USAGE"}, f"{database_name}.requests": {"SELECT", "INSERT"},
               f"{database_name}.failed_requests": {"SELECT", "INSERT"}}
    for grant in connection.execute(sa.text(f"SHOW GRANTS FOR '{app_user}'@'%'")).scalars():
        match = re.match(r"GRANT (.+?) ON (.+?) TO ", grant)
        scope = match.group(2).replace("`", "") if match else None
        privileges = {value.strip() for value in match.group(1).split(",")} if match else set()
        if (not match or scope not in allowed or not privileges <= allowed[scope]
                or "WITH GRANT OPTION" in grant):
            raise RuntimeError("The existing application user has broader grants. Use a dedicated databaseUser; "
                               "its password and existing grants were left unchanged.")


def initialize(engine, database_name=None, app_user=None, app_password=None):
    metadata.create_all(engine, checkfirst=True)
    verify_schema(engine)
    if app_user:
        db_name, app_user = identifier(database_name), identifier(app_user)
        if not app_password:
            raise ValueError("An application password is required.")
        # Identifiers are validated, while the password stays a bound parameter.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f"CREATE USER IF NOT EXISTS '{app_user}'@'%' IDENTIFIED BY :password"),
                               {"password": app_password})
            verify_application_grants(connection, db_name, app_user)
            connection.execute(sa.text(f"ALTER USER '{app_user}'@'%' IDENTIFIED BY :password"),
                               {"password": app_password})
            for table in ("requests", "failed_requests"):
                connection.execute(sa.text(f"GRANT SELECT, INSERT ON `{db_name}`.`{table}` TO '{app_user}'@'%'") )
            verify_application_grants(connection, db_name, app_user)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("runtime.json"))
    parser.add_argument("--gcloud-auth", action="store_true")
    parser.add_argument("--admin-user")
    parser.add_argument("--grant-user")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    engine, connector = create_engine(config, credentials=GcloudCredentials() if args.gcloud_auth else None,
        user=args.admin_user, password=os.environ.get("HW5_ADMIN_PASSWORD"))
    try:
        initialize(engine, config["database"]["name"], args.grant_user, os.environ.get("HW5_APP_PASSWORD"))
        print("Schema verified. Existing rows were preserved.")
    finally:
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()
