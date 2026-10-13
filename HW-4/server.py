"""Direct HTTP server on a Compute Engine VM; no function serving gateway."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from google.cloud import logging as cloud_logging, pubsub_v1, storage
from service import FileService, RequestLogger


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass  # FileService emits structured request records instead.

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def dispatch(self):
            body = b""
            if self.command == "POST":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 0 or size > 16384 or self.headers.get("Transfer-Encoding"):
                        raise ValueError("POST needs a Content-Length of at most 16384 bytes.")
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError("Incomplete POST body.")
                except ValueError as error:
                    self.send_error(400, str(error))
                    return
            response = service.handle(self.command, self.path, dict(self.headers), body, self.client_address[0])
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Request-Id", response.request_id)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_CONNECT = do_OPTIONS = do_TRACE = do_PATCH = dispatch
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("runtime.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    # The SDKs obtain short-lived credentials from the VM metadata service.
    bucket = storage.Client(project=config["projectId"]).bucket(config["bucketName"])
    logger = RequestLogger(cloud_logging.Client(project=config["projectId"]).logger(config["logName"]))
    service = FileService(config, bucket, pubsub_v1.PublisherClient(), logger)
    with ThreadingHTTPServer(("0.0.0.0", config["port"]), make_handler(service)) as server:
        logger.emit("INFO", "Web server ready.", event_type="server_ready", port=config["port"])
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
