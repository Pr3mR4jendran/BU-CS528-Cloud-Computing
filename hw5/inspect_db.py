"""Print table structure, row counts and recent records without changing data."""
import argparse
import json
from pathlib import Path

import sqlalchemy as sa

from credentials import GcloudCredentials
from database import create_engine, failed_requests, requests, verify_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name(".state") / "runtime.json")
    parser.add_argument("--gcloud-auth", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    engine, connector = create_engine(config, credentials=GcloudCredentials() if args.gcloud_auth else None)
    try:
        verify_schema(engine)
        output = {}
        with engine.connect() as connection:
            for table in (requests, failed_requests):
                output[table.name] = dict(primary_key=["request_id"], columns=list(table.columns.keys()),
                    count=connection.scalar(sa.select(sa.func.count()).select_from(table)),
                    recent=[dict(row) for row in connection.execute(
                        sa.select(table).order_by(table.c.request_time.desc()).limit(args.limit)).mappings()])
        print(json.dumps(output, indent=2, default=str, ensure_ascii=False))
    finally:
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()
