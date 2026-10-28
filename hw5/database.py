"""Cloud SQL connection pooling and atomic two-table request persistence."""
import os
import re

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

metadata = sa.MetaData()
timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
requests = sa.Table("requests", metadata,
    sa.Column("request_id", sa.String(36), primary_key=True),
    sa.Column("request_time", timestamp, nullable=False),
    sa.Column("country", sa.String(128)), sa.Column("client_ip", sa.String(45)),
    sa.Column("gender", sa.String(16)), sa.Column("age", sa.String(16)),
    sa.Column("income", sa.String(16)), sa.Column("is_banned", sa.Boolean, nullable=False),
    sa.Column("time_of_day", sa.Time), sa.Column("client_time", sa.DateTime),
    sa.Column("requested_file", sa.Text, nullable=False),
    sa.Column("status_code", sa.SmallInteger, nullable=False),
    sa.Column("peer_ip", sa.String(45)), sa.Column("method", sa.String(16), nullable=False),
    sa.Column("run_id", sa.String(64), nullable=False),
    sa.Column("traffic_category", sa.String(16), nullable=False),
    sa.CheckConstraint("status_code BETWEEN 100 AND 599", name="valid_response_status"),
    mysql_engine="InnoDB", mysql_charset="utf8mb4")
sa.Index("ix_requests_run_traffic", requests.c.run_id, requests.c.traffic_category)
failed_requests = sa.Table("failed_requests", metadata,
    sa.Column("request_id", sa.String(36), sa.ForeignKey("requests.request_id"), primary_key=True),
    sa.Column("request_time", timestamp, nullable=False),
    sa.Column("requested_file", sa.Text, nullable=False),
    sa.Column("error_code", sa.SmallInteger, nullable=False),
    sa.CheckConstraint("error_code BETWEEN 100 AND 599 AND error_code <> 200", name="non_success_status"),
    mysql_engine="InnoDB", mysql_charset="utf8mb4")


def identifier(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
        raise ValueError("Database/user names must use lowercase letters, digits and underscores (max 32).")
    return value


def create_engine(config, *, credentials=None, user=None, password=None):
    """Return (engine, connector); engine owns a pool, callers own transactions."""
    from google.cloud.sql.connector import Connector, IPTypes
    from google.cloud import secretmanager

    settings = config["database"]
    if password is None:
        password = os.environ.get("HW5_DB_PASSWORD")
    if password is None:
        with secretmanager.SecretManagerServiceClient(credentials=credentials) as secrets:
            password = secrets.access_secret_version(
                request={"name": settings["passwordSecret"]}).payload.data.decode("utf-8")
    connector = Connector(ip_type=IPTypes.PUBLIC, credentials=credentials, refresh_strategy="LAZY")

    def connect():
        return connector.connect(settings["instanceConnectionName"], "pymysql",
            user=user or settings["user"], password=password, db=settings["name"],
            charset="utf8mb4", connect_timeout=15, read_timeout=30, write_timeout=30,
            init_command="SET time_zone = '+00:00'")

    engine = sa.create_engine("mysql+pymysql://", creator=connect, pool_size=4, max_overflow=0,
                             pool_timeout=30, pool_recycle=1800, pool_pre_ping=True, hide_parameters=True)
    return engine, connector


def verify_schema(engine):
    inspector = sa.inspect(engine)
    for table in (requests, failed_requests):
        if not inspector.has_table(table.name):
            raise RuntimeError(f"Missing table {table.name}; run setup_schema.py first.")
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        if actual != set(table.columns.keys()):
            raise RuntimeError(f"Schema mismatch in {table.name}; existing data was preserved.")
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != ["request_id"]:
            raise RuntimeError(f"Missing expected primary key in {table.name}.")
    foreign_keys = inspector.get_foreign_keys("failed_requests")
    if not any(fk["constrained_columns"] == ["request_id"] and fk["referred_table"] == "requests"
               and fk["referred_columns"] == ["request_id"] for fk in foreign_keys):
        raise RuntimeError("failed_requests needs its request_id foreign key.")


class RequestStore:
    def __init__(self, engine):
        self.engine = engine

    def insert(self, record):
        with self.engine.begin() as connection:
            connection.execute(requests.insert(), record)
            if record["status_code"] != 200:
                connection.execute(failed_requests.insert(), dict(
                    request_id=record["request_id"], request_time=record["request_time"],
                    requested_file=record["requested_file"], error_code=record["status_code"]))
