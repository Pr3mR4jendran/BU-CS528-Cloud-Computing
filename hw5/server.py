"""HW4 HTTP behavior with mandatory, atomic Cloud SQL request logging."""
import argparse
import json
import os
from pathlib import Path
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.cloud import logging as cloud_logging, pubsub_v1, storage

from database import RequestStore, create_engine, verify_schema
from request_data import extract_record
from metrics import Timings, TimingJournal
from experiment_control import CONTROL_PATH, ExperimentControl
from service import FileService, RequestLogger, Result


class FailureJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def append(self, record):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())


def make_handler(service, store, failure_journal, timing_journal=None, experiment_control=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def persist(self, response, body=b"", received_at=None):
            with self.timing.measure("processing"):
                record = extract_record(service, getattr(self, "command", "UNKNOWN") or "UNKNOWN",
                    getattr(self, "path", ""), dict(getattr(self, "headers", {})), body,
                    self.client_address[0], response, received_at or self.timing.received_at)
            self.record, self.persisted = record, False
            try:
                with self.timing.measure("database"):
                    store.insert(record)
                self.persisted = True
            except Exception as error:
                record["intended_status_code"] = record["status_code"]
                record["status_code"] = 503
                service.logger.emit("ERROR", "SQL insert failed; request is not counted as persisted.",
                    event_type="database_write_failed", event_id=response.request_id, error_type=type(error).__name__)
                try:
                    failure_journal.append(record)
                except Exception as journal_error:
                    service.logger.emit("CRITICAL", "Database failure journal could not be written.",
                        event_type="failure_journal_failed", event_id=response.request_id,
                        error_type=type(journal_error).__name__)
                return Result(503, b"Request logging is temporarily unavailable.\n", request_id=response.request_id)
            return response

        def respond(self, response):
            completed = False
            try:
                with self.timing.measure("response"):
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.content_type)
                    self.send_header("Content-Length", str(len(response.body)))
                    self.send_header("X-Request-Id", response.request_id)
                    if getattr(self, "path", "") == "/healthz":
                        self.send_header("X-HW5-Bundle", os.environ.get("HW5_BUNDLE_SHA256", "local"))
                        self.send_header("X-HW5-Run", service.config.get("runId", "development"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if getattr(self, "command", "") != "HEAD":
                        self.wfile.write(response.body)
                    self.wfile.flush()
                    completed = True
            except (BrokenPipeError, ConnectionResetError, socket.timeout) as error:
                service.logger.emit("ERROR", "Client disconnected during the response.",
                    event_type="response_write_failed", event_id=response.request_id,
                    error_type=type(error).__name__)
            finally:
                self.close_connection = True
                if timing_journal:
                    try:
                        timing_journal.append(self.timing.finish(self.record, persisted=self.persisted,
                            completed=completed, body_bytes=len(response.body) if completed and self.command != "HEAD" else 0))
                    except Exception as error:
                        service.logger.emit("CRITICAL", "Timing journal write failed; measurement is incomplete.",
                            event_type="timing_journal_failed", event_id=response.request_id, error_type=type(error).__name__)

        def send_error(self, code, message=None, explain=None):
            # Covers parser rejections and unsupported custom HTTP methods too.
            self.timing = Timings()
            response = Result(code, b"Invalid or unsupported HTTP request.\n", request_id=str(uuid.uuid4()))
            self.respond(self.persist(response))

        def read_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding is unsupported.")
            if self.command != "POST":
                return b""
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 16384:
                raise ValueError("POST body is too large or has a negative length.")
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete POST body.")
            return body

        def dispatch(self):
            self.timing = Timings()
            body = b""
            try:
                with self.timing.measure("processing"):
                    body = self.read_body()
            except (ValueError, socket.timeout):
                response = Result(400, b"Invalid request body.\n", request_id=str(uuid.uuid4()))
            else:
                try:
                    if self.path == CONTROL_PATH and experiment_control:
                        with self.timing.measure("processing"):
                            response = experiment_control.handle(self.command, dict(self.headers), body,
                                self.client_address[0], str(uuid.uuid4()))
                    else:
                        response = service.handle(self.command, self.path, dict(self.headers), body, self.client_address[0], self.timing)
                except ValueError:
                    response = Result(400, b"Invalid request target.\n", request_id=str(uuid.uuid4()))
                except Exception as error:
                    service.logger.emit("ERROR", "Unexpected request processing failure.",
                        event_type="request_processing_failed", error_type=type(error).__name__)
                    response = Result(500, b"Request processing failed.\n", request_id=str(uuid.uuid4()))
            self.respond(self.persist(response, body, self.timing.received_at))

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_CONNECT = do_OPTIONS = do_TRACE = do_PATCH = dispatch
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("runtime.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    engine, connector = create_engine(config)
    try:
        verify_schema(engine)  # Refuse to serve requests against a missing/incompatible schema.
        storage_client = storage.Client(project=config["projectId"])
        bucket = storage_client.bucket(config["bucketName"])
        logger = RequestLogger(cloud_logging.Client(project=config["projectId"]).logger(config["logName"]))
        publisher = pubsub_v1.PublisherClient()
        service = FileService(config, bucket, publisher, logger)
        journal = FailureJournal(config.get("failureJournal", "/var/lib/hw5/database-failures.jsonl"))
        timings = TimingJournal(config.get("timingJournal", "/var/lib/hw5/timings.jsonl"))
        control = ExperimentControl(config, storage_client, timings, journal, args.config)
        with ThreadingHTTPServer(("0.0.0.0", config["port"]), make_handler(service, RequestStore(engine), journal, timings, control)) as server:
            logger.emit("INFO", "Web server and SQL schema ready.", event_type="server_ready", port=config["port"])
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()
