"""Serve bucket objects and publish forbidden-country events."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import mimetypes
import os
import uuid

from flask import Response
from google.api_core.exceptions import NotFound
from google.cloud import pubsub_v1, storage

FORBIDDEN_COUNTRIES = frozenset(
    country.casefold() for country in (
        "North Korea", "Iran", "Cuba", "Myanmar", "Iraq", "Libya",
        "Sudan", "Zimbabwe", "Syria",
    )
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def print_log(severity, message, **fields):
    print(json.dumps({"severity": severity, "message": message, **fields},
                     ensure_ascii=False), flush=True)


@dataclass(frozen=True)
class Settings:
    project_id: str
    bucket_name: str
    topic_path: str
    object_prefix: str = "pages/"

    @classmethod
    def from_environment(cls):
        return cls(os.environ["PROJECT_ID"], os.environ["BUCKET_NAME"],
                   os.environ["PUBSUB_TOPIC"], os.environ.get("OBJECT_PREFIX", "pages/"))


class FileService:
    def __init__(self, settings, storage_client, publisher):
        self.settings = settings
        self.bucket = storage_client.bucket(settings.bucket_name)
        self.publisher = publisher

    @classmethod
    def from_settings(cls, settings):
        # On Google Cloud these SDKs obtain the attached runtime account's identity.
        return cls(settings, storage.Client(project=settings.project_id),
                   pubsub_v1.PublisherClient())

    def object_name(self, requested):
        if not isinstance(requested, str):
            raise ValueError("A filename string is required.")
        name = requested.strip().lstrip("/")
        if name.startswith(self.settings.bucket_name + "/"):
            name = name[len(self.settings.bucket_name) + 1:]
        parts = name.split("/")
        if not name or "\\" in name or "\x00" in name or any(p in ("", ".", "..") for p in parts):
            raise ValueError("Invalid filename.")
        prefix = self.settings.object_prefix.strip("/") + "/"
        if name.startswith(prefix):
            return name
        # Bare filenames are convenient for POST and remain within the dataset.
        if len(parts) == 1:
            return prefix + name
        raise ValueError("The requested file must be within the dataset prefix.")

    @staticmethod
    def post_filename(request):
        if request.is_json:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("POST requires a JSON object with a filename field.")
            return payload.get("filename")
        if request.mimetype == "application/x-www-form-urlencoded":
            return request.form.get("filename")
        if request.mimetype in ("text/plain", "application/octet-stream", ""):
            return request.get_data(as_text=True)
        raise ValueError("Use JSON, a filename form field, or a plain-text filename.")

    def handle(self, request):
        event_id = str(uuid.uuid4())
        country = " ".join(request.headers.get("X-country", "").split())
        fields = dict(event_id=event_id, timestamp=utc_now(), method=request.method,
                      path=request.path, country=country or "unknown")
        trace = request.headers.get("X-Cloud-Trace-Context", "").split("/")[0]
        if trace:
            fields["logging.googleapis.com/trace"] = f"projects/{self.settings.project_id}/traces/{trace}"

        def response(body, status, content_type="text/plain; charset=utf-8"):
            return Response(body, status=status, content_type=content_type,
                            headers={"X-Request-Id": event_id})

        if request.method not in ("GET", "POST"):
            print_log("WARNING", "HTTP method is not implemented.",
                      event_type="unsupported_method", status=501, **fields)
            return response("HTTP method is not implemented.\n", 501)

        requested = request.path
        parse_error = None
        if request.method == "POST":
            try:
                requested = self.post_filename(request)
            except ValueError as error:
                parse_error = str(error)
        try:
            filename = self.object_name(requested) if not parse_error else None
        except ValueError as error:
            filename, parse_error = None, str(error)

        # Country denial precedes bucket access, even for a nonexistent file.
        if country.casefold() in FORBIDDEN_COUNTRIES:
            event = dict(event_type="forbidden_request", status=400, filename=filename,
                         message=f"Permission denied: request from {country} for {filename or request.path}.",
                         **fields)
            try:
                future = self.publisher.publish(
                    self.settings.topic_path, json.dumps(event).encode("utf-8"),
                    event_type="forbidden_request", event_id=event_id)
                future.result(timeout=15)
            except Exception as error:
                print_log("ERROR", "Could not publish the forbidden request.",
                          event_type="publish_failed", status=503, error_type=type(error).__name__, **fields)
                return response("Notification service is temporarily unavailable.\n", 503)
            print_log("WARNING", event["message"], filename=filename,
                      event_type="forbidden_request", status=400, **fields)
            return response("Permission denied.\n", 400)

        if parse_error:
            status = 400 if request.method == "POST" else 404
            print_log("WARNING", parse_error, event_type="invalid_filename", status=status, **fields)
            return response(parse_error + "\n", status)

        try:
            blob = self.bucket.blob(filename)
            content = blob.download_as_bytes(timeout=20)
        except NotFound:
            print_log("WARNING", "Requested file was not found.", filename=filename,
                      event_type="file_not_found", status=404, **fields)
            return response("File not found.\n", 404)
        except Exception as error:
            print_log("ERROR", "Could not retrieve the requested file.", filename=filename,
                      event_type="storage_error", status=500, error_type=type(error).__name__, **fields)
            return response("File storage is temporarily unavailable.\n", 500)

        content_type = blob.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        print_log("INFO", "Requested file served.", filename=filename,
                  event_type="file_served", status=200, bytes=len(content), **fields)
        return response(content, 200, content_type)
