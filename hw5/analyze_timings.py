"""Summarize measured journal stages and optionally reconcile a curl demonstration."""
import argparse
import json
from pathlib import Path

from metrics import STAGES, distribution

ROOT = Path(__file__).resolve().parent


def summarize(path, *, request_ids=None, run_id=None, category=None):
    selected, malformed, seen = [], [], set()
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(number)
                continue
            if ((request_ids is not None and row["request_id"] not in request_ids)
                    or (run_id and row["run_id"] != run_id) or (category and row["traffic_category"] != category)):
                continue
            if row["request_id"] in seen:
                raise ValueError("Duplicate timing request ID; do not concatenate overlapping snapshots.")
            seen.add(row["request_id"])
            selected.append(row)
    groups = {}
    for name, rows in (("all", selected), ("successful", [row for row in selected if row["status_code"] == 200]),
                       ("unsuccessful", [row for row in selected if row["status_code"] != 200])):
        groups[name] = dict(requests=len(rows), **{stage: distribution([row["stages_ns"][stage] for row in rows
             if row["stages_ns"][stage] is not None]) for stage in STAGES},
             total=distribution([row["total_ns"] for row in rows]), other=distribution([row["other_ns"] for row in rows]))
    missing = sorted(set(request_ids or []) - seen)
    failures = sum(not row["persisted"] for row in selected)
    disconnected = sum(not row["response_completed"] for row in selected)
    return dict(groups=groups, missing_request_ids=missing, malformed_lines=malformed,
        sql_write_failures=failures, incomplete_responses=disconnected,
        complete=bool(selected) and not (missing or malformed or failures or disconnected),
        definitions=dict(units="milliseconds", p95="nearest rank", processing="body/header/path processing and SQL record extraction",
            storage="uncached Cloud Storage read, including failed attempts", database="pool checkout, insert(s), commit/rollback",
            response="server write and flush; not client delivery", other="logging, Pub/Sub and uninstrumented work",
            total="dispatch/parser-error handling through write/flush completion, excludes timing-journal serialization and flush"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, nargs="?")
    demos = parser.add_mutually_exclusive_group()
    demos.add_argument("--demo", type=Path, help="A completed demonstration manifest.json")
    demos.add_argument("--latest-demo", action="store_true", help="Use the latest successfully completed local curl demonstration.")
    parser.add_argument("--run-id")
    parser.add_argument("--category", choices=("workload", "development", "health", "other"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    journal = args.journal or Path(json.loads((ROOT / ".state" / "latest-export.json").read_text())["directory"]) / "timings.jsonl"
    ids = None
    if args.latest_demo:
        args.demo = Path(json.loads((ROOT / ".state" / "latest-demo.json").read_text())["manifest"])
    if args.demo:
        demo = json.loads(args.demo.read_text())
        if not demo.get("complete"):
            parser.error("The selected demonstration is incomplete.")
        ids = set(demo["request_ids"])
    report = summarize(journal, request_ids=ids, run_id=args.run_id, category=args.category)
    output = args.output or journal.with_name("timing-summary.json")
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Saved {output}")
    if not report["complete"]:
        raise SystemExit("Measurement incomplete; inspect the reported diagnostics.")


if __name__ == "__main__":
    main()
