"""Fit both HW6 classifiers and write predictions for two separate held-out sets."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime
import gzip
from hashlib import sha256
from ipaddress import ip_address, IPv4Address, IPv6Address
import json
from pathlib import Path
from time import perf_counter


WORKLOAD_RUN = "full-exp-20261027T214058Z"
WORKLOAD_SIZE = 99_703
SPLIT_SEED = 528


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    address: IPv4Address | IPv6Address
    country: str
    income: str
    age: str | None
    gender: str | None
    hour: int | None

    @classmethod
    def from_row(cls, row):
        if not all(row[key] for key in ("request_id", "client_ip", "country", "income")):
            raise ValueError("A workload row is missing an ID, client IP, country or income label.")
        client_time = row["client_time"]
        if isinstance(client_time, str):
            client_time = datetime.fromisoformat(client_time)
        gender = row["gender"] if row["gender"] in ("Male", "Female") else None
        return cls(row["request_id"], ip_address(row["client_ip"]), row["country"],
                   row["income"], row["age"], gender, client_time.hour if client_time else None)


def snapshot_rows(path):
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row["run_id"] == WORKLOAD_RUN and row["traffic_category"] == "workload":
                yield row


def database_rows(path):
    from google.cloud import secretmanager
    from google.cloud.sql.connector import Connector, IPTypes
    from pymysql.cursors import DictCursor

    config = json.loads(path.read_text(encoding="utf-8-sig"))["database"]
    with secretmanager.SecretManagerServiceClient() as secrets:
        password = secrets.access_secret_version(
            request={"name": config["passwordSecret"]}
        ).payload.data.decode("utf-8")
    query = Path(__file__).with_name("model_data.sql").read_text(encoding="utf-8")
    with Connector(ip_type=IPTypes.PUBLIC, refresh_strategy="LAZY") as connector:
        with closing(connector.connect(config["instanceConnectionName"], "pymysql",
                                       user=config["user"], password=password, db="hw6",
                                       charset="utf8mb4", read_timeout=300,
                                       cursorclass=DictCursor)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, {"run_id": WORKLOAD_RUN})
                yield from cursor


def split_bucket(address, namespace):
    digest = sha256(f"hw6-{namespace}:{address}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def split_rows(rows, task):
    train, validation, test = [], [], []
    for row in rows:
        bucket = split_bucket(row.address, f"{task}-{SPLIT_SEED}")
        (train if bucket < 64 else validation if bucket < 80 else test).append(row)
    return train, validation, test


def majority(counts):
    return min(counts, key=lambda label: (-counts[label], label))


def prefix_key(address):
    # Use /24 for the IPv4 workload and /48 for IPv6 addresses.
    return address.version, int(address) >> (8 if address.version == 4 else 80)


def fit_country(rows):
    votes = defaultdict(Counter)
    for row in rows:
        votes[prefix_key(row.address)][row.country] += 1
    return {key: majority(counts) for key, counts in votes.items()}, majority(Counter(r.country for r in rows))


class IncomeModel:
    """Estimate class probabilities by age, then apply gender and hour likelihoods."""

    def __init__(self, rows):
        self.age_counts = defaultdict(Counter)
        self.gender_counts = defaultdict(Counter)
        self.hour_counts = defaultdict(Counter)
        totals = Counter()
        for row in rows:
            totals[row.income] += 1
            self.age_counts[row.age][row.income] += 1
            self.gender_counts[row.age, row.gender][row.income] += 1
            self.hour_counts[row.age, row.hour][row.income] += 1
        self.labels = sorted(totals)
        self.default = majority(totals)

    def predict_age(self, row):
        counts = self.age_counts.get(row.age)
        return majority(counts) if counts else self.default

    def predict(self, row):
        counts = self.age_counts.get(row.age)
        if not counts:
            return self.default
        genders = self.gender_counts.get((row.age, row.gender), {})
        hours = self.hour_counts.get((row.age, row.hour), {})
        # Laplace smoothing includes an unknown gender and an unknown hour.
        return max(self.labels, key=lambda label: (
            counts[label]
            * (genders.get(label, 0) + 1) / (counts[label] + 3)
            * (hours.get(label, 0) + 1) / (counts[label] + 25)
        ))


def score(rows, predictions, target):
    correct = sum(getattr(row, target) == guess for row, guess in zip(rows, predictions, strict=True))
    return {"rows": len(rows), "correct": correct, "accuracy": correct / len(rows) if rows else None}


def income_scores(model, rows, predictions):
    return {
        **score(rows, predictions, "income"),
        "majority_accuracy": score(rows, [model.default] * len(rows), "income")["accuracy"],
        "age_only_accuracy": score(rows, [model.predict_age(row) for row in rows], "income")["accuracy"],
    }


def income_sensitivity(development_rows):
    scores = {}
    for seed in (529, 530, 531):
        train, validation = [], []
        for row in development_rows:
            bucket = split_bucket(row.address, f"income-sensitivity-{seed}")
            (train if bucket < 80 else validation).append(row)
        model = IncomeModel(train)
        scores[str(seed)] = score(validation, [model.predict(row) for row in validation], "income")["accuracy"]
    return scores


def explain_income(train, test, sensitivity):
    by_age = defaultdict(Counter)
    for row in train:
        by_age[row.age][row.income] += 1
    age_profiles = {
        age or "(missing)": {"counts": dict(counts), "most_common_share": max(counts.values()) / counts.total()}
        for age, counts in by_age.items()
    }
    bands = sorted({len(counts) for counts in by_age.values()})
    shares = [profile["most_common_share"] for profile in age_profiles.values()]
    accuracy = test["accuracy"]
    outcome = (f"{(0.35 - accuracy) * 100:.2f} percentage points below" if accuracy < 0.35
               else "at or above")
    explanation = (
        f"Income accuracy was {accuracy:.2%} ({test['correct']:,}/{test['rows']:,}), "
        f"{outcome} the 35% target. The majority-class baseline scored {test['majority_accuracy']:.2%} "
        f"and the age-only baseline scored {test['age_only_accuracy']:.2%}. "
        f"In training, the observed number of income bands per age group was "
        f"{', '.join(map(str, bands))}, and the most frequent band within each age group accounted for "
        f"{min(shares):.2%} to {max(shares):.2%} of that group's rows. "
        f"Age narrowed the possible labels, and gender and client hour supplied only a small additional gain. "
        f"Three development-only resplits scored {min(sensitivity.values()):.2%} to "
        f"{max(sensitivity.values()):.2%}. Together, these measurements explain the observed accuracy "
        f"on this workload."
    )
    return age_profiles, explanation


def country_results(rows):
    train, validation, test = split_rows(rows, "country")
    prefixes, fallback = fit_country(train)
    validation_predictions = [prefixes.get(prefix_key(row.address), fallback) for row in validation]
    start = perf_counter()
    prefixes, fallback = fit_country(train + validation)
    fit_seconds = perf_counter() - start
    start = perf_counter()
    test_predictions = [prefixes.get(prefix_key(row.address), fallback) for row in test]
    predict_seconds = perf_counter() - start
    test_summary = score(test, test_predictions, "country")
    test_summary["matched_prefix_rows"] = sum(prefix_key(row.address) in prefixes for row in test)
    test_summary["fallback_rows"] = len(test) - test_summary["matched_prefix_rows"]
    metrics = {
        "model": "IP-prefix majority",
        "training_rows_before_refit": len(train),
        "validation": score(validation, validation_predictions, "country"),
        "test": test_summary,
        "refit_prefixes": len(prefixes),
        "refit_seconds": fit_seconds,
        "test_predict_seconds": predict_seconds,
    }
    return test, test_predictions, metrics


def income_results(rows):
    train, validation, test = split_rows(rows, "income")
    model = IncomeModel(train)
    validation_summary = income_scores(model, validation, [model.predict(row) for row in validation])
    development = train + validation
    start = perf_counter()
    model = IncomeModel(development)
    fit_seconds = perf_counter() - start
    start = perf_counter()
    test_predictions = [model.predict(row) for row in test]
    predict_seconds = perf_counter() - start
    test_summary = income_scores(model, test, test_predictions)
    confusion = defaultdict(Counter)
    for row, guess in zip(test, test_predictions, strict=True):
        confusion[row.income][guess] += 1
    test_summary["confusion_counts_by_actual_label"] = confusion
    sensitivity = income_sensitivity(development)
    age_profiles, explanation = explain_income(train, test_summary, sensitivity)
    metrics = {
        "model": "Age-conditioned naive Bayes with gender and client hour",
        "training_rows_before_refit": len(train),
        "validation": validation_summary,
        "development_validation_accuracy_by_seed": sensitivity,
        "training_income_distribution_by_age": age_profiles,
        "explanation": explanation,
        "test": test_summary,
        "refit_seconds": fit_seconds,
        "test_predict_seconds": predict_seconds,
    }
    return test, test_predictions, metrics


def write_predictions(path, target, rows, predictions):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("request_id", f"actual_{target}", f"predicted_{target}"))
        writer.writerows((row.request_id, getattr(row, target), guess)
                         for row, guess in zip(rows, predictions, strict=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--snapshot", type=Path, help="Saved HW5 requests.jsonl.gz for local development")
    inputs.add_argument("--db-config", type=Path, help="VM runtime JSON with a Cloud SQL reader and password secret")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = snapshot_rows(args.snapshot) if args.snapshot else database_rows(args.db_config)
    rows = sorted((Request.from_row(row) for row in source), key=lambda row: row.request_id)
    if len(rows) != WORKLOAD_SIZE or len({row.request_id for row in rows}) != WORKLOAD_SIZE:
        parser.error(f"Expected {WORKLOAD_SIZE:,} distinct HW5 workload requests, got {len(rows):,} rows.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics = {"workload_run": WORKLOAD_RUN, "workload_rows": len(rows), "split_seed": SPLIT_SEED}
    for target, evaluate in (("country", country_results), ("income", income_results)):
        test, predictions, metrics[target] = evaluate(rows)
        write_predictions(args.output_dir / f"{target}_predictions.csv", target, test, predictions)
    summary = json.dumps(metrics, indent=2, sort_keys=True)
    (args.output_dir / "metrics.json").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
