"""Audit the retained HW5 data or copy it into a verified 3NF schema."""

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError


ROOT = Path(__file__).resolve().parent
WORKLOAD_RUN = "full-exp-20261027T214058Z"
EXPECTED_WORKLOAD = 99_703
INCOME_GROUPS = {
    "0-10k", "10k-20k", "20k-40k", "40k-60k", "60k-100k", "100k-150k", "150k-250k", "250k+",
}
SOURCE_ERRORS = (
    "workload_ip_conflicts", "ip_format_conflicts", "ban_conflicts", "invalid_ban_flags",
    "time_conflicts", "failure_conflicts", "orphan_failures", "failure_count_mismatch",
)


def sql_statements(filename):
    # The supplied SQL files contain no semicolons inside their statements.
    return [statement.strip() for statement in (ROOT / filename).read_text(encoding="utf-8").split(";")
            if statement.strip()]


def run_sql(connection, statement, description, parameters=None):
    print(description, flush=True)
    try:
        if parameters is None:
            return connection.exec_driver_sql(statement)
        return connection.execute(text(statement), parameters)
    except OperationalError as error:
        raise RuntimeError(f"{description} failed: {error.orig}") from None


def inspect_source(connection):
    summary = {}
    checks = ("Checking source requests", "Checking IP mappings", "Checking country policy",
              "Checking linked failures")
    for statement, check in zip(sql_statements("audit.sql"), checks, strict=True):
        summary.update(run_sql(connection, statement, check, {"run_id": WORKLOAD_RUN}).mappings().one())
    summary["failure_count_mismatch"] = abs(summary["source_non_200"] - summary["source_failures"])
    rows = run_sql(connection, """
        SELECT income, COUNT(*) AS records,
               SUM(client_ip IS NULL OR country IS NULL OR income IS NULL) AS missing_labels
        FROM hw5.requests
        WHERE run_id = :run_id AND traffic_category = 'workload'
        GROUP BY income
    """, "Checking the saved workload", {"run_id": WORKLOAD_RUN}).mappings().all()
    summary["workload_run"] = WORKLOAD_RUN
    summary["workload_rows"] = sum(row["records"] for row in rows)
    summary["workload_income_counts"] = {row["income"]: row["records"] for row in rows}
    summary["workload_rows_missing_model_fields"] = sum(int(row["missing_labels"]) for row in rows)
    problems = {name: summary[name] for name in SOURCE_ERRORS if summary[name]}
    if problems:
        raise RuntimeError(f"HW5 source violates migration assumptions: {problems}")
    if (summary["workload_rows"] != EXPECTED_WORKLOAD
            or summary["workload_rows_missing_model_fields"]
            or set(summary["workload_income_counts"]) != INCOME_GROUPS):
        raise RuntimeError(
            f"HW5 workload differs from the saved run of {EXPECTED_WORKLOAD:,} rows: {summary}"
        )
    return summary


def prepare_schema(connection):
    create_database, *create_tables = sql_statements("schema.sql")
    run_sql(connection, create_database, "Preparing hw6")
    schema = inspect(connection)
    if schema.has_table("request_countries_without_ip", schema="hw6"):
        run_sql(connection, """
            RENAME TABLE hw6.request_countries_without_ip TO hw6.request_country_observations
        """, "Upgrading the earlier HW6 country table")
    for statement in create_tables:
        run_sql(connection, statement, f"Preparing {statement.split()[5]}")


def migrate(engine):
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        if connection.exec_driver_sql("SELECT GET_LOCK('cs528_hw6_migration', 0)").scalar_one() != 1:
            raise RuntimeError("Another HW6 migration holds the MySQL lock.")
        connection.commit()
        try:
            # MySQL commits DDL separately from the audited data copy.
            with connection.begin():
                prepare_schema(connection)
            with connection.begin():
                # Lock both source tables for this transaction so validation and INSERT SELECT
                # see the same rows, even if a legacy writer reconnects.
                run_sql(connection, "SELECT COUNT(*) FROM hw5.requests FOR SHARE",
                        "Locking source requests for a consistent copy").scalar_one()
                run_sql(connection, "SELECT COUNT(*) FROM hw5.failed_requests FOR SHARE",
                        "Locking source failures for a consistent copy").scalar_one()
                summary = inspect_source(connection)
                expected = {
                    "requests": summary["source_requests"],
                    "countries": summary["source_countries"],
                    "client_ips": summary["source_ips"],
                    "request_country_observations": (summary["source_countries_without_ip"]
                                                     + summary["source_countries_for_ambiguous_ips"]),
                }
                counts = table_counts(connection)
                if not any(counts.values()):
                    for statement in sql_statements("migrate.sql"):
                        run_sql(connection, statement, f"Copying {statement.split()[2]}")
                    counts = table_counts(connection)
                    action = "copied"
                else:
                    action = "already_migrated"
                if counts != expected:
                    raise RuntimeError(f"HW6 table counts differ from the source: {counts}; expected {expected}")
                verification, = sql_statements("verify.sql")
                mismatches = run_sql(connection, verification, "Comparing migrated requests with HW5").scalar_one()
                if mismatches:
                    raise RuntimeError(f"{mismatches:,} HW6 requests differ from their HW5 source rows.")
                failure_count = connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM hw6.requests WHERE status_code <> 200"
                ).scalar_one()
                if failure_count != summary["source_failures"]:
                    raise RuntimeError(f"HW6 derives {failure_count:,} failures from request status.")
                return {
                    **summary,
                    "migration": action,
                    "target_tables": counts,
                    "reconstructed_request_mismatches": mismatches,
                    "reconstructed_failures": failure_count,
                }
        finally:
            if not connection.invalidated:
                connection.exec_driver_sql("DO RELEASE_LOCK('cs528_hw6_migration')")


def table_counts(connection):
    return {
        table: connection.exec_driver_sql(f"SELECT COUNT(*) FROM hw6.{table}").scalar_one()
        for table in ("requests", "countries", "client_ips", "request_country_observations")
    }
