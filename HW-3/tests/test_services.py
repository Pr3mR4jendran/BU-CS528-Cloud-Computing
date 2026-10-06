import contextlib
from datetime import datetime, timedelta, timezone
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "cloud-function"), str(ROOT / "subscriber")]

import functions_framework
from google.api_core.exceptions import NotFound, PermissionDenied, PreconditionFailed
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request

from service import FileService, Settings, FORBIDDEN_COUNTRIES
from audit import AuditWriter, process_message
from authentication import impersonated_credentials

verification_spec = importlib.util.spec_from_file_location("verification", ROOT / "verify-results.py")
verification = importlib.util.module_from_spec(verification_spec)
verification_spec.loader.exec_module(verification)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.blob = Mock(content_type="text/html")
        self.blob.download_as_bytes.return_value = b"<html>sample</html>"
        self.bucket = Mock()
        self.bucket.blob.return_value = self.blob
        storage_client = Mock()
        storage_client.bucket.return_value = self.bucket
        self.publisher = Mock()
        service = FileService(Settings("project", "bucket", "projects/project/topics/events"),
                              storage_client, self.publisher)
        self.app = functions_framework.create_app(target="serve_file", source=str(ROOT / "cloud-function/main.py"))
        # Exercise the framework's real route registration for HEAD, OPTIONS and CONNECT.
        sys.modules["main"]._service = service
        self.client = self.app.test_client()

    def request(self, method, path="/pages/0.html", **kwargs):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            response = self.client.open(path, method=method, **kwargs)
        logs = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status"], response.status_code)
        self.assertEqual(logs[0]["event_id"], response.headers["X-Request-Id"])
        return response, logs[0]

    def test_get_and_client_bucket_path(self):
        for path in ("/0.html", "/pages/0.html", "/bucket/pages/0.html"):
            with self.subTest(path=path):
                response, log = self.request("GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b"<html>sample</html>")
                self.assertEqual(response.mimetype, "text/html")
                self.bucket.blob.assert_called_with("pages/0.html")
                self.assertEqual(log["event_type"], "file_served")

    def test_post_payloads(self):
        payloads = [dict(json={"filename": "0.html"}),
                    dict(data={"filename": "pages/0.html"}),
                    dict(data="0.html", content_type="text/plain")]
        for payload in payloads:
            with self.subTest(payload=payload):
                response, _ = self.request("POST", "/", **payload)
                self.assertEqual(response.status_code, 200)
                self.bucket.blob.assert_called_with("pages/0.html")

    def test_missing_file_is_404_and_logged(self):
        self.blob.download_as_bytes.side_effect = NotFound("missing")
        response, log = self.request("GET", "/pages/12000.html")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(log["event_type"], "file_not_found")

    def test_unsupported_methods_reach_function(self):
        for method in ("PUT", "DELETE", "HEAD", "CONNECT", "OPTIONS", "TRACE", "PATCH"):
            with self.subTest(method=method):
                response, log = self.request(method)
                self.assertEqual(response.status_code, 501)
                self.assertEqual(log["event_type"], "unsupported_method")
                if method == "HEAD":
                    self.assertEqual(response.data, b"")
        self.bucket.blob.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_all_forbidden_countries_publish_without_reading(self):
        for country in FORBIDDEN_COUNTRIES:
            with self.subTest(country=country):
                response, log = self.request("GET", headers={"X-country": "  " + country.upper() + "  "})
                self.assertEqual(response.status_code, 400)
                arguments, attributes = self.publisher.publish.call_args
                event = json.loads(arguments[1])
                self.assertEqual(event["event_id"], log["event_id"])
                self.assertEqual(event["status"], 400)
                self.assertEqual(attributes["event_type"], "forbidden_request")
        self.bucket.blob.assert_not_called()

    def test_audit_prefix_is_not_exposed(self):
        response, _ = self.request("GET", "/hw3-logs/forbidden-requests.jsonl")
        self.assertEqual(response.status_code, 404)
        self.bucket.blob.assert_not_called()

    def test_invalid_post(self):
        for payload in (None, [], {}, {"filename": "../secret"}, {"filename": 1}):
            with self.subTest(payload=payload):
                response, _ = self.request("POST", "/", data=json.dumps(payload), content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.bucket.blob.assert_not_called()

    def test_infrastructure_errors_do_not_masquerade_as_missing_files(self):
        self.blob.download_as_bytes.side_effect = PermissionDenied("denied")
        response, log = self.request("GET")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(log["event_type"], "storage_error")
        self.publisher.publish.return_value.result.side_effect = RuntimeError("publish failed")
        response, log = self.request("GET", headers={"X-country": "Iran"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(log["event_type"], "publish_failed")


def event(event_id="first"):
    return dict(event_id=event_id, timestamp="2026-10-06T00:00:00+00:00",
                event_type="forbidden_request", status=400, country="Iran", method="GET",
                filename="pages/0.html", message="Permission denied: Iran.")


class MemoryBucket:
    def __init__(self):
        self.text = None
        self.generation = 0
        self.conflict = False
        self.uploads = 0

    def blob(self, name):
        bucket = self

        class Blob:
            def reload(self, **kwargs):
                if bucket.text is None:
                    raise NotFound("missing")
                self.generation = bucket.generation

            def download_as_text(self, if_generation_match, **kwargs):
                if if_generation_match != bucket.generation:
                    raise PreconditionFailed("changed")
                return bucket.text

            def upload_from_string(self, text, if_generation_match, **kwargs):
                if bucket.conflict:
                    bucket.conflict = False
                    bucket.text = json.dumps(event("concurrent")) + "\n"
                    bucket.generation += 1
                if if_generation_match != bucket.generation:
                    raise PreconditionFailed("changed")
                bucket.text = text
                bucket.generation += 1
                bucket.uploads += 1

        return Blob()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.bucket = MemoryBucket()
        self.writer = AuditWriter(self.bucket, "hw3-logs/events.jsonl", sleep=lambda _: None)

    def test_create_append_and_duplicate_after_restart(self):
        self.assertTrue(self.writer.append(event(), "1"))
        self.assertTrue(self.writer.append(event("second"), "2"))
        restarted = AuditWriter(self.bucket, "hw3-logs/events.jsonl")
        self.assertFalse(restarted.append(event(), "3"))
        entries = [json.loads(line) for line in self.bucket.text.splitlines()]
        self.assertEqual([entry["event_id"] for entry in entries], ["first", "second"])
        self.assertEqual(self.bucket.uploads, 2)

    def test_retry_preserves_concurrent_append(self):
        self.bucket.conflict = True
        self.assertTrue(self.writer.append(event(), "1"))
        self.assertEqual([json.loads(line)["event_id"] for line in self.bucket.text.splitlines()],
                         ["concurrent", "first"])

    def test_corrupt_existing_log_is_preserved(self):
        self.bucket.text = "not json\n"
        self.bucket.generation = 1
        with self.assertRaises(ValueError):
            self.writer.append(event(), "1")
        self.assertEqual(self.bucket.text, "not json\n")
        self.assertEqual(self.bucket.uploads, 0)

    def test_ack_only_after_successful_persistence(self):
        message = Mock(data=json.dumps(event()).encode(), attributes={}, message_id="1")
        message.ack.side_effect = lambda: self.assertIsNotNone(self.bucket.text)
        self.assertTrue(process_message(message, self.writer, Mock()))
        message.ack.assert_called_once()
        message.nack.assert_not_called()

    def test_failed_write_is_nacked(self):
        message = Mock(data=json.dumps(event()).encode(), attributes={}, message_id="1")
        writer = Mock()
        writer.append.side_effect = PermissionDenied("denied")
        self.assertFalse(process_message(message, writer, Mock()))
        message.nack.assert_called_once()
        message.ack.assert_not_called()

    def test_invalid_event_is_acknowledged_without_writing(self):
        message = Mock(data=b"not json", attributes={}, message_id="1")
        writer = Mock()
        self.assertFalse(process_message(message, writer, Mock()))
        writer.append.assert_not_called()
        message.ack.assert_called_once()


class AuthenticationTests(unittest.TestCase):
    @patch("authentication.shutil.which", return_value="gcloud.cmd")
    @patch("authentication.subprocess.run")
    def test_refresh_uses_shared_account_and_explicit_token_lifetime(self, run, which):
        run.return_value = Mock(returncode=0, stdout="test-token\n", stderr="")
        config = dict(serviceAccountEmail="shared@project.iam.gserviceaccount.com",
                      provisioningAccount="user@example.com", projectId="project")
        credentials = impersonated_credentials(config)
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        credentials.refresh(Request())
        self.assertEqual(credentials.token, "test-token")
        self.assertGreater(credentials.expiry, before + timedelta(minutes=54))
        arguments = run.call_args.args[0]
        self.assertIn("--lifetime=3600", arguments)
        self.assertIn("--impersonate-service-account=" + config["serviceAccountEmail"], arguments)

    @patch("authentication.shutil.which", return_value="gcloud.cmd")
    @patch("authentication.subprocess.run")
    def test_failed_refresh_never_exposes_stdout(self, run, which):
        run.return_value = Mock(returncode=1, stdout="secret-token", stderr="denied")
        credentials = impersonated_credentials(dict(serviceAccountEmail="shared", provisioningAccount="user", projectId="project"))
        with self.assertRaises(RefreshError) as caught:
            credentials.refresh(Request())
        self.assertNotIn("secret-token", str(caught.exception))


class VerificationTests(unittest.TestCase):
    def test_polling_uses_a_fresh_blob_to_follow_new_generations(self):
        old, new = Mock(), Mock()
        old.download_as_text.return_value = ""
        new.download_as_text.return_value = json.dumps(event()) + "\n"
        bucket = Mock()
        bucket.blob.side_effect = [old, new]
        text, entries = verification.wait_for_audit(bucket, "audit.jsonl", {"first"}, sleep=lambda _: None)
        self.assertEqual(bucket.blob.call_count, 2)
        self.assertEqual(entries[0]["event_id"], "first")

    def test_duplicate_audit_entries_fail_verification(self):
        bucket = Mock()
        bucket.blob.return_value.download_as_text.return_value = (json.dumps(event()) + "\n") * 2
        with self.assertRaises(RuntimeError):
            verification.wait_for_audit(bucket, "audit.jsonl", {"first"})

    def test_client_output_accepts_html_and_country_denials(self):
        text = ("Requesting  /pages/0.html  from  example.run.app 443\n200 OK\n"
                "x-request-id: served\n\nb'<!DOCTYPE html>sample'\n"
                "Requesting  /pages/1.html  from  example.run.app 443\n400 Bad Request\n"
                "x-request-id: denied\n\nb'Permission denied.\\n'\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.txt"
            path.write_text(text, encoding="utf-16")
            results = verification.client_results(path, 2)
        self.assertEqual([result["actual"] for result in results], [200, 400])
        self.assertEqual(results[1]["filename"], "pages/1.html")

    def test_client_exit_success_does_not_hide_http_errors(self):
        text = "Requesting  /pages/0.html  from  example.run.app 443\n404 Not Found\nb'error'\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.txt"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verification.client_results(path, 1)


if __name__ == "__main__":
    unittest.main()
