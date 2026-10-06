"""Run the second service on the laptop using the first service's identity."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

from google.auth.transport.requests import Request
from google.cloud import pubsub_v1, storage
from google.cloud.pubsub_v1.subscriber.scheduler import ThreadScheduler

from audit import AuditWriter, process_message
from authentication import impersonated_credentials


def emit(severity, message, **fields):
    print(json.dumps(dict(severity=severity, message=message, **fields), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    parser.add_argument("--max-messages", type=int, default=0, help="Stop after this many persisted events; 0 keeps running.")
    parser.add_argument("--stop-file", type=Path, help="Stop gracefully when this file exists.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    credentials = impersonated_credentials(config)
    credentials.refresh(Request())
    storage_client = storage.Client(project=config["projectId"], credentials=credentials)
    writer = AuditWriter(storage_client.bucket(config["bucketName"]), config["auditObject"])
    stop = threading.Event()
    count = 0

    def callback(message):
        nonlocal count
        if process_message(message, writer, emit):
            count += 1
            if args.max_messages and count >= args.max_messages:
                stop.set()

    with pubsub_v1.SubscriberClient(credentials=credentials) as subscriber:
        scheduler = ThreadScheduler(ThreadPoolExecutor(max_workers=1))
        future = subscriber.subscribe(config["subscriptionPath"], callback=callback,
            flow_control=pubsub_v1.types.FlowControl(max_messages=1),
            scheduler=scheduler, await_callbacks_on_shutdown=True)
        emit("INFO", "Subscriber ready on the local laptop.", event_type="subscriber_ready",
             service_account=config["serviceAccountEmail"], subscription=config["subscriptionPath"],
             audit_object=config["auditObject"])
        try:
            while not stop.wait(1):
                if args.stop_file and args.stop_file.exists():
                    break
                if future.done():
                    future.result()
                    break
        except KeyboardInterrupt:
            pass
        finally:
            future.cancel()
            future.result(timeout=30)
            emit("INFO", "Subscriber stopped.", event_type="subscriber_stopped", persisted_events=count)


if __name__ == "__main__":
    main()
