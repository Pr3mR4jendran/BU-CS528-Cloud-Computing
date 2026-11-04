"""Publish the two VM prediction files, then the run's completion metrics."""

import json
from pathlib import Path
from time import sleep

from google.api_core.exceptions import Forbidden
from google.cloud import storage


def main():
    config = json.loads(Path("runtime.json").read_text(encoding="utf-8"))
    client = storage.Client(project=config["projectId"])
    bucket = client.bucket(config["bucketName"])
    for filename, mime in (("country_predictions.csv", "text/csv"),
                           ("income_predictions.csv", "text/csv"),
                           ("metrics.json", "application/json")):
        blob = bucket.blob(config["outputPrefix"] + filename)
        for attempt in range(4):
            try:
                blob.upload_from_filename(str(Path("results") / filename), content_type=mime,
                                          if_generation_match=0, timeout=60)
                break
            except Forbidden:
                if attempt == 3:
                    raise
                print("Waiting for the temporary bucket permission to propagate.", flush=True)
                sleep(15)
        print(f"Uploaded gs://{bucket.name}/{blob.name}", flush=True)


if __name__ == "__main__":
    main()
