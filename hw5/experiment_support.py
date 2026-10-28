"""Shared experiment validation and atomic JSON checkpoints."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

PROJECT = "bu-cs528-prem-project"
PREFIX = "cs528-hw5"
CLIENT_HASH = "1c4f429609445da8d31270c36f23732b2c81b449639b2efefb7a777f1213bcc6"


def now():
    return datetime.now(timezone.utc).isoformat()


def run_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,56}", value):
        raise ValueError("Experiment IDs must contain 1-56 letters, digits, underscores or hyphens.")
    return value


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def public_job(job):
    return {key: value for key, value in job.items() if key != "controlToken"}
