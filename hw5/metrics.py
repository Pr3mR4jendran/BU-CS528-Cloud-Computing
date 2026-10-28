"""Per-request monotonic timings; skipped stages remain null, never zero samples."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time

STAGES = ("processing", "storage", "database", "response")


class Timings:
    def __init__(self, clock=time.perf_counter_ns):
        self.clock = clock
        self.started = clock()
        self.received_at = datetime.now(timezone.utc)
        self.stages = dict.fromkeys(STAGES)

    @contextmanager
    def measure(self, stage):
        start = self.clock()
        try:
            yield
        finally:
            self.stages[stage] = (self.stages[stage] or 0) + self.clock() - start

    def finish(self, record, *, persisted, completed, body_bytes):
        total = self.clock() - self.started
        return dict(request_id=record["request_id"], run_id=record["run_id"],
            traffic_category=record["traffic_category"], received_at=self.received_at.isoformat(),
            status_code=record["status_code"], intended_status_code=record.get("intended_status_code", record["status_code"]),
            persisted=persisted, response_completed=completed, body_bytes=body_bytes,
            stages_ns=self.stages, total_ns=total,
            other_ns=max(0, total - sum(value or 0 for value in self.stages.values())))


class TimingJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def append(self, record):
        # Flush each complete JSON line. FailureJournal separately fsyncs SQL failures.
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def distribution(values):
    values = sorted(values)
    if not values:
        return dict(samples=0, mean_ms=None, median_ms=None, p95_ms=None)
    count = len(values)
    middle = count // 2
    median = values[middle] if count % 2 else (values[middle - 1] + values[middle]) / 2
    return dict(samples=count, mean_ms=sum(values) / count / 1e6,
                median_ms=median / 1e6, p95_ms=values[math.ceil(.95 * count) - 1] / 1e6)
