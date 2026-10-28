"""HW4 file service, independent of its HTTP transport."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import mimetypes
import uuid
from urllib.parse import parse_qs, unquote, urlsplit
from contextlib import nullcontext

from google.api_core.exceptions import NotFound

FORBIDDEN_COUNTRIES = frozenset(country.casefold() for country in (
    "North Korea", "Iran", "Cuba", "Myanmar", "Iraq", "Libya",
    "Sudan", "Zimbabwe", "Syria",
))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Result:
    status: int
    body: bytes
    content_type: str = "text/plain; charset=utf-8"
    request_id: str = ""


class RequestLogger:
    """Keep stdout evidence and explicitly deliver errors to Cloud Logging."""
    def __init__(self, cloud_logger):
        self.cloud_logger = cloud_logger

    def emit(self, severity, message, **fields):
        payload = dict(message=message, **fields)
        print(json.dumps(dict(severity=severity, **payload), ensure_ascii=False), flush=True)
        if severity in ("WARNING", "ERROR", "CRITICAL"):
            try:
                self.cloud_logger.log_struct(payload, severity=severity)
            except Exception as error:
                # Preserve the response and leave a visible diagnostic for verification.
                print(json.dumps(dict(severity="ERROR", event_type="cloud_logging_failed",
                    event_id=fields.get("event_id"), error_type=type(error).__name__,
                    message=str(error))), flush=True)


class FileService:
    def __init__(self, config, bucket, publisher, logger):
        self.config, self.bucket = config, bucket
        self.publisher, self.logger = publisher, logger
        self.prefix = config["objectPrefix"].strip("/") + "/"

    def object_name(self, requested):
        if not isinstance(requested, str):
            raise ValueError("A filename string is required.")
        name = requested.strip().lstrip("/")
        bucket_prefix = self.config["bucketName"] + "/"
        if name.startswith(bucket_prefix):
            name = name[len(bucket_prefix):]
        parts = name.split("/")
        if not name or "\\" in name or "\x00" in name or any(p in ("", ".", "..") for p in parts):
            raise ValueError("Invalid filename.")
        if name.startswith(self.prefix):
            return name
        if len(parts) == 1:
            return self.prefix + name
        raise ValueError("The requested file must be within the dataset prefix.")

    @staticmethod
    def post_filename(body, content_type):
        mime = content_type.split(";", 1)[0].strip().lower()
        try:
            text = body.decode("utf-8")
            if mime == "application/json":
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("POST requires a JSON object with a filename field.")
                return payload.get("filename")
            if mime == "application/x-www-form-urlencoded":
                return parse_qs(text).get("filename", [None])[0]
            if mime in ("text/plain", "application/octet-stream", ""):
                return text
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid UTF-8 or JSON POST payload.") from error
        raise ValueError("Use JSON, a filename form field, or a plain-text filename.")

    def process_request(self, method, target, headers, body, client_ip):
        event_id = str(uuid.uuid4())
        headers = {key.casefold(): value for key, value in headers.items()}
        country = " ".join(headers.get("x-country", "").split())
        path = urlsplit(target).path
        fields = dict(event_id=event_id, timestamp=utc_now(), method=method,
                      path=path, country=country or "unknown", client_ip=client_ip)

        filename, parse_error = None, None
        try:
            requested = self.post_filename(body, headers.get("content-type", "")) if method == "POST" else unquote(path, errors="strict")
            filename = self.object_name(requested)
        except (ValueError, UnicodeError) as error:
            parse_error = str(error)
        return event_id, fields, country, path, filename, parse_error

    def read_file(self, filename):
        blob = self.bucket.blob(filename)
        content = blob.download_as_bytes(timeout=20)
        return content, blob.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def handle(self, method, target, headers, body=b"", client_ip="unknown", timing=None):
        with timing.measure("processing") if timing else nullcontext():
            event_id, fields, country, path, filename, parse_error = self.process_request(method, target, headers, body, client_ip)

        def result(status, text, severity, event_type, **extra):
            self.logger.emit(severity, text.strip(), status=status, event_type=event_type, **fields, **extra)
            return Result(status, text.encode(), request_id=event_id)

        if method not in ("GET", "POST"):
            return result(501, "HTTP method is not implemented.\n", "ERROR", "unsupported_method")
        if method == "GET" and path == "/healthz":
            return Result(200, b"ready\n", request_id=event_id)

        if country.casefold() in FORBIDDEN_COUNTRIES:
            event = dict(event_type="forbidden_request", status=400, filename=filename,
                message=f"Permission denied: request from {country} for {filename or path}.", **fields)
            self.logger.emit("CRITICAL", event["message"], **{k: v for k, v in event.items() if k != "message"})
            try:
                self.publisher.publish(self.config["topicPath"], json.dumps(event).encode(),
                    event_type="forbidden_request", event_id=event_id).result(timeout=15)
            except Exception as error:
                return result(503, "Notification service is temporarily unavailable.\n", "ERROR",
                              "publish_failed", error_type=type(error).__name__)
            return Result(400, b"Permission denied.\n", request_id=event_id)
        if parse_error:
            return result(400 if method == "POST" else 404, parse_error + "\n", "WARNING", "invalid_filename")
        try:
            with timing.measure("storage") if timing else nullcontext():
                content, content_type = self.read_file(filename)
        except NotFound:
            return result(404, "File not found.\n", "WARNING", "file_not_found", filename=filename)
        except Exception as error:
            return result(500, "File storage is temporarily unavailable.\n", "ERROR", "storage_error",
                          filename=filename, error_type=type(error).__name__)
        self.logger.emit("INFO", "Requested file served.", status=200, event_type="file_served",
                         filename=filename, bytes=len(content), **fields)
        return Result(200, content, content_type, event_id)
