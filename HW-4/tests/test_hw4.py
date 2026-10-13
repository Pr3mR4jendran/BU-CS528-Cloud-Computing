import contextlib
import copy
import io
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from google.api_core.exceptions import NotFound
from audit import AuditWriter, process_message
import deploy
from server import make_handler
from service import FileService, FORBIDDEN_COUNTRIES, RequestLogger
from run_client import client_responses


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config.example.json").read_text())
        cls.config.update(projectId="test-project", bucketName="test-bucket", topicPath="projects/test-project/topics/test")

    def setUp(self):
        self.blob = Mock(content_type="text/html")
        self.blob.download_as_bytes.return_value = b"<html>sample\r\n</html>"
        self.bucket = Mock()
        self.bucket.blob.return_value = self.blob
        self.publisher, self.cloud_logger = Mock(), Mock()
        self.service = FileService(self.config, self.bucket, self.publisher, RequestLogger(self.cloud_logger))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method="GET", path="/pages/0.html", body=None, headers=None):
        with contextlib.redirect_stdout(io.StringIO()):
            connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            status, data, request_id = response.status, response.read(), response.getheader("X-Request-Id")
            connection.close()
        self.assertTrue(request_id)
        return status, data

    def test_get_and_three_post_formats_return_original_bytes(self):
        for method, body, mime in (("GET", None, ""),
            ("POST", '{"filename":"pages/0.html"}', "application/json"),
            ("POST", "filename=0.html", "application/x-www-form-urlencoded"),
            ("POST", "0.html", "text/plain")):
            with self.subTest(method=method, mime=mime):
                status, data = self.request(method, body=body, headers={"Content-Type": mime})
                self.assertEqual((status, data), (200, self.blob.download_as_bytes.return_value))

    def test_missing_file_logs_structured_warning(self):
        self.blob.download_as_bytes.side_effect = NotFound("missing")
        self.assertEqual(self.request()[0], 404)
        args, kwargs = self.cloud_logger.log_struct.call_args
        self.assertEqual(kwargs["severity"], "WARNING")
        self.assertEqual(args[0]["status"], 404)

    def test_all_seven_methods_over_real_socket_and_error_severity(self):
        for method in ("PUT", "DELETE", "HEAD", "CONNECT", "OPTIONS", "TRACE", "PATCH"):
            with self.subTest(method=method):
                status, body = self.request(method)
                self.assertEqual(status, 501)
                self.assertEqual(self.cloud_logger.log_struct.call_args.kwargs["severity"], "ERROR")
                if method == "HEAD":
                    self.assertEqual(body, b"")
        self.bucket.blob.assert_not_called()

    def test_all_countries_case_normalization_and_denial_before_storage(self):
        for country in FORBIDDEN_COUNTRIES:
            with self.subTest(country=country):
                self.assertEqual(self.request(headers={"X-country": country.upper()})[0], 400)
                self.assertEqual(self.cloud_logger.log_struct.call_args.kwargs["severity"], "CRITICAL")
                event = json.loads(self.publisher.publish.call_args.args[1])
                self.assertEqual(event["country"], country.upper())
                self.assertEqual(event["status"], 400)
        self.bucket.blob.assert_not_called()

    def test_storage_and_publish_failures_are_not_mislabeled(self):
        self.blob.download_as_bytes.side_effect = RuntimeError("storage")
        self.assertEqual(self.request()[0], 500)
        self.publisher.publish.side_effect = RuntimeError("publish")
        self.assertEqual(self.request(headers={"X-country": "Iran"})[0], 503)

    def test_private_audit_prefix_and_invalid_json(self):
        self.assertEqual(self.request(path="/hw4-logs/private.jsonl")[0], 404)
        self.assertEqual(self.request("POST", body="{", headers={"Content-Type": "application/json"})[0], 400)
        self.bucket.blob.assert_not_called()


class AuditTests(unittest.TestCase):
    def test_duplicate_after_write_before_ack_is_not_appended(self):
        event = dict(event_id="id", timestamp="time", country="Iran", method="GET", message="denied",
                     event_type="forbidden_request", status=400)
        blob = Mock(generation=1)
        blob.download_as_text.return_value = json.dumps(event) + "\n"
        bucket = Mock()
        bucket.blob.return_value = blob
        writer = AuditWriter(bucket, "hw4-logs/run/forbidden.jsonl")
        message = Mock(data=json.dumps(event).encode(), attributes={}, message_id="message")
        self.assertTrue(process_message(message, writer, Mock()))
        blob.upload_from_string.assert_not_called()
        message.ack.assert_called_once()

    def test_failed_audit_is_nacked(self):
        event = dict(event_id="id", timestamp="time", country="Iran", method="GET", message="denied",
                     event_type="forbidden_request", status=400)
        message = Mock(data=json.dumps(event).encode(), attributes={}, message_id="message")
        writer = Mock()
        writer.append.side_effect = RuntimeError("cannot write")
        self.assertFalse(process_message(message, writer, Mock()))
        message.nack.assert_called_once()
        message.ack.assert_not_called()


class ClientVerificationTests(unittest.TestCase):
    def test_response_ids_bodies_and_missing_responses_are_visible(self):
        output = "Requesting /pages/0.html from 127.0.0.1 8080\n200 OK\nx-request-id: first\nb'<!DOCTYPE html>sample'\nRequesting /pages/1.html from 127.0.0.1 8080\n400 Bad Request\nx-request-id: second\nb'Permission denied.\\n'\nRequesting /pages/2.html from 127.0.0.1 8080\n"
        results = client_responses(output)
        self.assertEqual(len(results), 3)
        self.assertTrue(results[0]["bodyValid"])
        self.assertTrue(results[1]["bodyValid"])
        self.assertIsNone(results[2]["status"])
        self.assertIsNone(results[2]["event_id"])

    def test_error_html_is_not_a_successful_download(self):
        results = client_responses("Requesting /pages/0.html from host 8080\n200 OK\nx-request-id: id\nb'internal server error'\n")
        self.assertFalse(results[0]["bodyValid"])


class DeploymentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.example.json").read_text())
        self.config.update(projectId="test-project", bucketName="existing-data", account="user@example.com")

    def test_missing_detection_does_not_hide_permission_or_auth_errors(self):
        self.assertTrue(deploy.is_missing("NOT_FOUND: topic does not exist"))
        self.assertTrue(deploy.is_missing("ERROR: No URLs matched: gs://bucket/missing.jsonl"))
        self.assertTrue(deploy.is_missing("The following URLs matched no objects or files: gs://bucket/missing"))
        self.assertFalse(deploy.is_missing("PERMISSION_DENIED: resource not found or permission denied"))
        self.assertFalse(deploy.is_missing("Reauthentication failed"))

    def test_deleted_account_permission_error_requires_authorized_absence_check(self):
        gc = deploy.Gcloud.__new__(deploy.Gcloud)
        gc.run = Mock(side_effect=[Mock(returncode=1, stderr="PERMISSION_DENIED", stdout=""),
                                   Mock(returncode=0, stderr="", stdout="[]")])
        self.assertIsNone(gc.describe(["iam", "service-accounts", "describe", "owned@test.iam.gserviceaccount.com"]))
        self.assertEqual(gc.run.call_args.args[0][:3], ["iam", "service-accounts", "list"])

    def test_existing_account_permission_error_is_not_treated_as_absence(self):
        gc = deploy.Gcloud.__new__(deploy.Gcloud)
        gc.run = Mock(side_effect=[Mock(returncode=1, stderr="PERMISSION_DENIED", stdout=""),
                                   Mock(returncode=0, stderr="", stdout='[{"email":"owned@test.iam.gserviceaccount.com"}]')])
        with self.assertRaisesRegex(RuntimeError, "PERMISSION_DENIED"):
            gc.describe(["iam", "service-accounts", "describe", "owned@test.iam.gserviceaccount.com"])

    def test_cleanup_waits_for_deleted_iam_account_to_disappear_from_list(self):
        resource = deploy.resources(self.config, 'owner', Path('bootstrap'))[0]
        gc = deploy.Gcloud.__new__(deploy.Gcloud)
        gc.run = Mock(side_effect=[Mock(returncode=1, stderr='PERMISSION_DENIED', stdout=''),
                                  Mock(returncode=0, stderr='', stdout='[{"email":"stale-account"}]'),
                                  Mock(returncode=1, stderr='PERMISSION_DENIED', stdout=''),
                                  Mock(returncode=0, stderr='', stdout='[]')])
        with patch('deploy.time.sleep') as delay, contextlib.redirect_stdout(io.StringIO()):
            result = deploy.verify_deleted_resource(gc, resource, deletion_confirmed=True)
        self.assertIsNone(result)
        self.assertEqual(gc.run.call_count, 4)
        delay.assert_called_once_with(1)

    def test_cleanup_does_not_hide_persistent_iam_permission_error(self):
        resource = deploy.resources(self.config, 'owner', Path('bootstrap'))[0]
        gc = Mock()
        gc.describe.side_effect = RuntimeError('PERMISSION_DENIED')
        with patch('deploy.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'PERMISSION_DENIED'):
                deploy.verify_deleted_resource(gc, resource, deletion_confirmed=True)
        self.assertEqual(gc.describe.call_count, 4)

    def test_cleanup_reports_account_that_remains_after_successful_delete(self):
        resource = deploy.resources(self.config, 'owner', Path('bootstrap'))[0]
        gc = Mock()
        gc.describe.return_value = dict(email=resource['name'])
        with patch('deploy.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            result = deploy.verify_deleted_resource(gc, resource, deletion_confirmed=True)
        self.assertEqual(result['email'], resource['name'])
        self.assertEqual(gc.describe.call_count, 4)

    def test_no_client_identity_and_no_shared_bucket_deletion(self):
        resources = deploy.resources(self.config, "owner", Path("bootstrap"))
        client = next(r for r in resources if r["kind"] == "instance" and r["name"].endswith("-client"))
        self.assertIn("--no-service-account", client["create"])
        self.assertFalse(any("buckets" in r["delete"] for r in resources))
        self.assertTrue(all(r["name"].startswith(self.config["prefix"]) for r in resources))

    def test_bootstrap_contains_source_and_automatic_services(self):
        with tempfile.TemporaryDirectory() as folder:
            deploy.bootstrap_scripts(self.config, "owner", Path(folder))
            for role in ("server", "reporter"):
                script = (Path(folder) / (role + ".sh")).read_text()
                self.assertIn("systemctl enable --now cs528-hw4-" + role, script)
                self.assertIn("User=hw4app", script)
                self.assertNotIn("auth application-default login", script)
                self.assertLess(len(script.encode()), 256 * 1024)

    def test_cleanup_refuses_foreign_owner_before_any_mutation(self):
        resource = deploy.resources(self.config, "owner", Path("bootstrap"))[0]
        state = dict(config=self.config, owner="owner", resources=[resource])
        fake = Mock()
        fake.describe.return_value = {"description": "someone else's account"}
        with patch.object(deploy, "Gcloud", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                deploy.down(state, Path("unused"))
        fake.run.assert_not_called()

    def test_collection_preserves_denial_logs_beyond_one_thousand(self):
        entries = [dict(jsonPayload=dict(event_id=str(i)), severity='CRITICAL') for i in range(1119)]

        def run(arguments, **kwargs):
            if arguments[:2] == ['logging', 'read']:
                limit = int(next(a.split('=', 1)[1] for a in arguments if a.startswith('--limit=')))
                return Mock(returncode=0, stdout=json.dumps(entries[:limit]), stderr='')
            return Mock(returncode=0, stdout='', stderr='')

        fake = Mock()
        fake.run.side_effect = run
        state = dict(config=self.config, owner='owner', resources=[],
                     runtime=deploy.runtime_config(self.config, 'owner'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(deploy, 'Gcloud', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
                deploy.collect(state, path)
            saved = json.loads((path / 'cloud-logs.json').read_text())
        self.assertEqual(saved, entries)

    def test_partial_cleanup_handles_only_created_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            resource = next(r for r in deploy.resources(self.config, "owner", root) if r["kind"] == "network")
            state = dict(config=self.config, owner="owner", resources=[resource], status="incomplete",
                         runtime=deploy.runtime_config(self.config, "owner"))
            remote = {"description": "Managed by CS528 HW4; owner=owner"}
            fake = Mock()
            present = [True]
            fake.describe.side_effect = lambda arguments: remote if arguments == resource["describe"] and present[0] else None
            def run(arguments, **kwargs):
                if arguments == resource["delete"]:
                    present[0] = False
                return Mock(returncode=0, stdout="", stderr="")
            fake.run.side_effect = run
            with patch.object(deploy, "ROOT", root), patch.object(deploy, "Gcloud", return_value=fake), patch.object(deploy, "collect"), contextlib.redirect_stdout(io.StringIO()):
                deploy.down(state, root / "evidence")
            self.assertEqual(state["status"], "deleted")
            fake.run.assert_any_call(resource["delete"])
            self.assertFalse(any("buckets" in call.args[0] and "delete" in call.args[0] for call in fake.run.call_args_list))


if __name__ == "__main__":
    unittest.main()
