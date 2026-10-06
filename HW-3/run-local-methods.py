"""Demonstrate every unsupported method with curl against the same function locally."""
import argparse
import contextlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import functions_framework
from google.cloud import pubsub_v1, storage
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "cloud-function"), str(ROOT / "subscriber")]
from authentication import impersonated_credentials
from service import FileService, Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    credentials = impersonated_credentials(config)
    settings = Settings(config["projectId"], config["bucketName"], config["topicPath"], config["objectPrefix"])
    publisher = pubsub_v1.PublisherClient(credentials=credentials)
    service = FileService(settings, storage.Client(project=config["projectId"], credentials=credentials), publisher)
    app = functions_framework.create_app(target="serve_file", source=str(ROOT / "cloud-function/main.py"))
    sys.modules["main"]._service = service
    server = make_server("127.0.0.1", 0, app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/pages/0.html"
    results = []
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is missing.")
    try:
        with (args.evidence / "local-method-logs.jsonl").open("w", encoding="utf-8") as logs:
            for method in ("PUT", "DELETE", "HEAD", "CONNECT", "OPTIONS", "TRACE", "PATCH"):
                headers = args.evidence / f"local-{method.lower()}.headers.txt"
                body = args.evidence / f"local-{method.lower()}.body"
                arguments = [curl, "--silent", "--show-error", "--noproxy", "127.0.0.1", "--http1.1",
                             "--max-time", "15", "--dump-header", str(headers), "--output", str(body),
                             "--write-out", "%{http_code}", "--header", "Content-Length: 0"]
                arguments.extend(["--head"] if method == "HEAD" else ["--request", method])
                with contextlib.redirect_stdout(logs):
                    response = subprocess.run(arguments + [url], capture_output=True, text=True, check=True, timeout=20)
                status = int(response.stdout)
                results.append(dict(method=method, url=url, expected=501, actual=status, passed=status == 501))
                print(f"Local {method:7} HTTP {status} (expected 501)")
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        publisher.stop()
    (args.evidence / "local-method-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if any(not result["passed"] for result in results):
        raise RuntimeError("A local method failed its 501 check.")


if __name__ == "__main__":
    main()
