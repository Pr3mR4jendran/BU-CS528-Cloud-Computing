"""Six required SQL analyses plus exact request/failure/timing reconciliation."""
import argparse
import gzip
import json
from pathlib import Path

import sqlalchemy as sa

from analyze_timings import summarize
from database import requests, failed_requests
from experiment_support import save_json


def boundary_ties(rows, limit):
    if len(rows) <= limit:
        return rows
    threshold = rows[limit - 1]["count"]
    return [row for row in rows if row["count"] >= threshold]


def analyze(engine, run_id, expected, journal, output, *, peer_ip=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    subset = sa.and_(requests.c.run_id == run_id, requests.c.traffic_category == "workload")
    with engine.connect() as connection:
        records = [dict(row) for row in connection.execute(sa.select(requests).where(subset)).mappings()]
        failures = [dict(row) for row in connection.execute(sa.select(failed_requests).join(requests,
            failed_requests.c.request_id == requests.c.request_id).where(subset)).mappings()]
        def groups(column):
            return [dict(value=row._mapping["value"], count=row._mapping["count"]) for row in connection.execute(
                sa.select(column.label("value"), sa.func.count().label("count")).where(subset)
                .group_by(column).order_by(sa.desc("count"), column))]
        status_codes = groups(requests.c.status_code)
        countries, ages, incomes, genders = [groups(column) for column in
            (requests.c.country, requests.c.age, requests.c.income, requests.c.gender)]
        invalid_client_rows = connection.scalar(sa.select(sa.func.count()).select_from(requests).where(
            requests.c.run_id == run_id, requests.c.peer_ip == peer_ip,
            requests.c.traffic_category.not_in(("workload", "control", "health")))) if peer_ip else 0
    ids = {row["request_id"] for row in records}
    expected_failures = {row["request_id"] for row in records if row["status_code"] != 200}
    linked_failures = {row["request_id"] for row in failures}
    codes = {row["request_id"]: row["status_code"] for row in records}
    consistent = expected_failures == linked_failures and all(row["error_code"] == codes[row["request_id"]] for row in failures)
    success = sum(row["status_code"] == 200 for row in records)
    banned = sum(row["is_banned"] for row in records)
    unexpected = sum(row["status_code"] != 200 and not (row["status_code"] == 400 and row["is_banned"]) for row in records)
    missing_fields = sum(any(row[key] is None for key in ("country", "client_ip", "gender", "age", "income", "client_time")) for row in records)
    timing = summarize(Path(journal), request_ids=ids, run_id=run_id, category="workload")
    timing_ok = timing["complete"] and timing["groups"]["all"]["requests"] == len(ids)
    report = dict(runId=run_id, expectedRequests=expected, actualRequests=len(records),
        complete=len(records) == expected and consistent and timing_ok and not (unexpected or missing_fields or invalid_client_rows),
        successful=success, unsuccessful=len(records) - success, statusCodes=status_codes, bannedCountries=banned,
        gender=genders, topFiveCountries=boundary_ties(countries, 5),
        mostFrequentAgeGroups=boundary_ties(ages, 1), mostFrequentIncomeGroups=boundary_ties(incomes, 1),
        failureRows=len(failures), failureLinksConsistent=consistent, unexpectedResponses=unexpected,
        missingDemographics=missing_fields, invalidClientRows=invalid_client_rows, timingComplete=timing_ok,
        ties="Includes every category tied at the fifth-country / most-frequent cutoff.")
    save_json(output / "statistics.json", report)
    save_json(output / "timing-summary.json", timing)
    with gzip.open(output / "requests.jsonl.gz", "wt", encoding="utf-8") as stream:
        for row in records:
            stream.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    save_json(output / "failed-requests.json", failures)
    return report, records


def main():
    from credentials import GcloudCredentials
    from database import create_engine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected", type=int, default=100000)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name(".state") / "runtime.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gcloud-auth", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    engine, connector = create_engine(config, credentials=GcloudCredentials() if args.gcloud_auth else None)
    try:
        report, _ = analyze(engine, args.run_id, args.expected, args.journal, args.output)
        print(json.dumps(report, indent=2))
        if not report["complete"]:
            raise SystemExit("Experiment reconciliation failed; inspect saved diagnostics.")
    finally:
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()
