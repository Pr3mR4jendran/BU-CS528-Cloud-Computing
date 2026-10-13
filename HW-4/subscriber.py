"""Forbidden-country reporting service, authenticated by its VM identity."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import threading

from google.cloud import pubsub_v1, storage
from google.cloud.pubsub_v1.subscriber.scheduler import ThreadScheduler
from audit import AuditWriter, process_message


def emit(severity, message, **fields):
    print(json.dumps(dict(severity=severity, message=message, **fields), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("runtime.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    writer = AuditWriter(storage.Client(project=config["projectId"]).bucket(config["bucketName"]), config["auditObject"])
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with pubsub_v1.SubscriberClient() as subscriber:
        future = subscriber.subscribe(config["subscriptionPath"],
            callback=lambda message: process_message(message, writer, emit),
            flow_control=pubsub_v1.types.FlowControl(max_messages=1),
            scheduler=ThreadScheduler(ThreadPoolExecutor(max_workers=1)), await_callbacks_on_shutdown=True)
        emit("INFO", "Reporter ready on its VM.", event_type="subscriber_ready",
             subscription=config["subscriptionPath"], audit_object=config["auditObject"])
        try:
            while not stop.wait(1):
                if future.done():
                    future.result()
                    break
        finally:
            future.cancel()
            future.result(timeout=45)
            emit("INFO", "Reporter stopped.", event_type="subscriber_stopped")


if __name__ == "__main__":
    main()
