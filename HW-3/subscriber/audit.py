"""Append JSON lines to one GCS object, with retry and duplicate protection."""
from datetime import datetime, timezone
import json
import time

from google.api_core.exceptions import NotFound, PreconditionFailed


class AuditWriter:
    def __init__(self, bucket, object_name, sleep=time.sleep):
        self.bucket = bucket
        self.object_name = object_name
        self.sleep = sleep

    def append(self, event, message_id):
        record = dict(event, pubsub_message_id=message_id,
                      received_at=datetime.now(timezone.utc).isoformat())
        for attempt in range(6):
            blob = self.bucket.blob(self.object_name)
            try:
                try:
                    blob.reload(timeout=20)
                except NotFound:
                    generation, previous = 0, ""
                else:
                    generation = int(blob.generation)
                    previous = blob.download_as_text(if_generation_match=generation, timeout=20)
                # IDs live in the same persistent object as the log entries.
                # A restart after writing but before ACK therefore cannot add a duplicate.
                for line in previous.splitlines():
                    if line.strip() and json.loads(line).get("event_id") == event["event_id"]:
                        return False
                separator = "" if not previous or previous.endswith("\n") else "\n"
                updated = previous + separator + json.dumps(record, ensure_ascii=False) + "\n"
                blob.upload_from_string(updated, content_type="application/x-ndjson",
                                        if_generation_match=generation, timeout=20)
                return True
            except (PreconditionFailed, NotFound):
                if attempt == 5:
                    raise
                self.sleep(min(0.1 * 2**attempt, 2))
        raise RuntimeError("Audit append exhausted its retries.")


def process_message(message, writer, emit):
    if message.attributes.get("event_type") == "setup_verification":
        emit("INFO", "Acknowledged a setup verification message.", event_type="setup_verification")
        message.ack()
        return False
    try:
        event = json.loads(message.data.decode("utf-8"))
        required = ("event_id", "timestamp", "country", "method", "message")
        if not isinstance(event, dict) or event.get("event_type") != "forbidden_request" or event.get("status") != 400:
            raise ValueError("Expected a forbidden_request event with status 400.")
        if any(not isinstance(event.get(field), str) or not event[field] for field in required):
            raise ValueError("The forbidden request is missing required string fields.")
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        emit("ERROR", "Invalid Pub/Sub event; no audit entry was written.",
             event_type="invalid_event", error_type=type(error).__name__, pubsub_message_id=message.message_id)
        message.ack()
        return False

    try:
        inserted = writer.append(event, message.message_id)
        if inserted:
            fields = {key: value for key, value in event.items() if key != "message"}
            emit("WARNING", event["message"], pubsub_message_id=message.message_id, **fields)
        else:
            emit("INFO", "Already persisted this event; duplicate delivery acknowledged.",
                 event_type="duplicate_delivery", event_id=event["event_id"])
        message.ack()
        return True
    except Exception as error:
        emit("ERROR", "Audit persistence failed; Pub/Sub will retry the event.",
             event_type="audit_write_failed", event_id=event["event_id"], error_type=type(error).__name__)
        message.nack()
        return False
