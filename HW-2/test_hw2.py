"""Offline correctness tests, independent of the supplied random 12K dataset."""
from collections import deque
from fractions import Fraction
import json
import io
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from hw2 import (PublicGCS, adjacency_bits, closeness, distance_summary,
                 distribution, download_with_transfer_manager, main,
                 pagerank, pagerank_step, parse_links, retryable_transfer_error, validate_objects)


def queue_bfs(adjacency, source):
    """Independent distance oracle, using a conventional queue instead of bit sets."""
    distances = {source: 0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for target in adjacency[node]:
            if target not in distances:
                distances[target] = distances[node] + 1
                queue.append(target)
    return len(distances) - 1, sum(distances.values())


def exact_pagerank(adjacency):
    """Independent rational Gaussian elimination for (I - .85 P^T)x = .15/n."""
    n = len(adjacency)
    damping = Fraction(17, 20)
    matrix = [[Fraction(int(i == j)) for j in range(n)] + [Fraction(3, 20 * n)]
              for i in range(n)]
    for j, targets in enumerate(adjacency):
        for i in targets:
            matrix[i][j] -= damping / len(targets)
    for column in range(n):
        pivot = next(i for i in range(column, n) if matrix[i][column])
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        divisor = matrix[column][column]
        matrix[column] = [value / divisor for value in matrix[column]]
        for i in range(n):
            if i != column:
                scale = matrix[i][column]
                matrix[i] = [a - scale * b for a, b in zip(matrix[i], matrix[column])]
    return [float(row[-1]) for row in matrix]


class PageRankTests(unittest.TestCase):
    def test_uniform_cycle(self):
        handout, stable, info = pagerank([[1], [2], [0]])
        for value in handout + stable:
            self.assertAlmostEqual(value, 1 / 3)
        self.assertEqual(info["handout_stop"]["iteration"], 1)

    def test_hand_computed_asymmetric_graph_and_bad_sum_stop(self):
        # A -> B,C; B -> C; C -> A. Starting at 1/3, first step is
        # A=1/3, B=23/120, C=19/40; their sum is already one.
        handout, stable, info = pagerank([[1, 2], [2], [0]], l1_tolerance=1e-13)
        for actual, expected in zip(handout, [1 / 3, 23 / 120, 19 / 40]):
            self.assertAlmostEqual(actual, expected, places=12)
        # Solving equations by hand gives A=686/1769, B=380/1769, C=703/1769.
        for actual, expected in zip(stable, [686 / 1769, 380 / 1769, 703 / 1769]):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertEqual(info["handout_stop"]["iteration"], 1)
        self.assertGreater(info["handout_stop"]["l1_change"], 0.2)
        self.assertGreater(info["stabilized_stop"]["iteration"], 1)

    def test_dangling_node_literal_formula(self):
        # A -> B, B has no links: fixed point A=.075, B=.13875.
        _, stable, _ = pagerank([[1], []])
        self.assertAlmostEqual(stable[0], 0.075)
        self.assertAlmostEqual(stable[1], 0.13875)
        self.assertLess(sum(stable), 1.0)

    def test_duplicate_edges_and_self_links(self):
        values = pagerank_step([[0, 1, 1], [0]], [0.5, 0.5])
        self.assertAlmostEqual(values[0], 0.075 + 0.85 * (0.5 / 3 + 0.5))
        self.assertAlmostEqual(values[1], 0.075 + 0.85 * (1 / 3))

    def test_matches_independent_rational_solver(self):
        graphs = [[[1, 2], [2], [0]], [[1], []], [[0, 1, 1], [0]],
                  [[], [], []], [[1], [0], [2]], [[0]]]
        for graph in graphs:
            with self.subTest(graph=graph):
                _, stable, info = pagerank(graph, l1_tolerance=1e-13)
                for actual, expected in zip(stable, exact_pagerank(graph)):
                    self.assertAlmostEqual(actual, expected, places=11)
                self.assertLess(info["stabilized_stop"]["fixed_point_l1_residual"], 1e-12)

    def test_iteration_limit_does_not_report_false_success(self):
        with self.assertRaises(RuntimeError):
            pagerank([[1, 2], [2], [0]], max_iterations=1)


class ClosenessTests(unittest.TestCase):
    def test_bidirectional_star_hand_solution(self):
        rows = closeness([[1, 2, 3], [0], [0], [0]])
        self.assertEqual(rows[0]["score"], 1.0)
        for row in rows[1:]:
            self.assertAlmostEqual(row["score"], 3 / 5)

    def test_directed_path_and_disconnected_convention(self):
        rows = closeness([[1], [2], []])
        self.assertAlmostEqual(rows[0]["score"], 2 / 3)
        self.assertEqual(rows[1]["score"], 0.0)
        self.assertAlmostEqual(rows[1]["wf_score"], 0.5)
        self.assertEqual(rows[2]["score"], 0.0)
        self.assertEqual(rows[0]["sum_finite_distances"], 3)

    def test_isolates_self_links_and_duplicates(self):
        rows = closeness([[0, 1, 1], [0], []])
        self.assertEqual(rows[0]["reachable_other_nodes"], 1)
        self.assertEqual(rows[0]["sum_finite_distances"], 1)
        self.assertEqual(rows[2]["wf_score"], 0)
        self.assertEqual(closeness([[]])[0]["score"], 0)

    def test_all_4096_directed_four_node_graphs_against_queue_bfs(self):
        # Exhaustive, not random: all subsets of the 12 possible non-self edges.
        edges = [(i, j) for i in range(4) for j in range(4) if i != j]
        for mask in range(1 << len(edges)):
            graph = [[] for _ in range(4)]
            for bit, (i, j) in enumerate(edges):
                if mask & (1 << bit):
                    graph[i].append(j)
            rows = adjacency_bits(graph)
            for source in range(4):
                self.assertEqual(distance_summary(rows, source), queue_bfs(graph, source))


class InputAndStatisticsTests(unittest.TestCase):
    def test_parser_case_quotes_duplicates_self_links(self):
        data = b'<a HREF="1.html">x</a><a href=\'1.html\'>y</a><a href="0.html">z</a>'
        self.assertEqual(list(parse_links(data, 2)), [1, 1, 0])
        self.assertEqual(list(parse_links(b"<html><body></body></html>", 2)), [])

    def test_invalid_target_rejected(self):
        for target in ["3.html", "https://example.com/1.html", "-1.html"]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                parse_links(f'<a href="{target}">x</a>'.encode(), 3)

    def test_statistics_known_quintiles(self):
        result = distribution([0, 1, 2, 3, 4, 5])
        self.assertEqual(result["mean"], 2.5)
        self.assertEqual(result["median"], 2.5)
        self.assertEqual(list(result["quintiles"].keys()), ["p20", "p40", "p60", "p80", "p100"])
        self.assertEqual(list(result["quintiles"].values()), [1, 2, 3, 4, 5])
        self.assertAlmostEqual(distribution([0, 10])["quintiles"]["p20"], 2.0)
        self.assertEqual(distribution([7])["median"], 7)

    def test_complete_listing_and_folder_marker(self):
        result = validate_objects([{"name": "pages/1.html"}, {"name": "pages/"},
                                   {"name": "pages/0.html"}], "pages/", 2)
        self.assertEqual(result[0]["name"], "pages/0.html")
        with self.assertRaises(ValueError):
            validate_objects([{"name": "pages/0.html"}], "pages/", 2)

    def test_public_api_pagination_and_generation(self):
        client = PublicGCS("example-bucket")
        pages = [json.dumps({"items": [{"name": "pages/0.html"}],
                             "nextPageToken": "next token"}).encode(),
                 json.dumps({"items": [{"name": "pages/1.html"}]}).encode()]
        with patch.object(client, "get", side_effect=pages) as get:
            self.assertEqual(len(client.list_objects("pages/")), 2)
            self.assertIn("pageToken=next+token", get.call_args.args[0])
        with patch.object(client, "get", return_value=b"test") as get:
            client.download({"name": "pages/0.html", "generation": "123"})
            self.assertIn("pages%2F0.html", get.call_args.args[0])
            self.assertIn("generation=123", get.call_args.args[0])


class TransferTests(unittest.TestCase):
    def dependencies(self):
        storage, manager = MagicMock(), MagicMock()
        manager.THREAD = "thread"
        client = storage.Client.create_anonymous_client.return_value
        bucket = client.bucket.return_value
        bucket.blob.side_effect = lambda name, generation: (name, generation)
        return storage, manager, client, bucket

    def test_transfer_batches_pin_generations_and_do_not_skip_files(self):
        storage, manager, client, bucket = self.dependencies()
        items = [{"name": f"pages/{i}.html", "generation": str(1000+i), "size": "1"}
                 for i in range(130)]

        def write_batch(pairs, **kwargs):
            self.assertEqual(kwargs["worker_type"], "thread")
            self.assertEqual(kwargs["max_workers"], 4)
            self.assertFalse(kwargs["raise_exception"])
            self.assertFalse(kwargs["skip_if_exists"])
            self.assertIsNone(kwargs["download_kwargs"]["retry"])
            self.assertEqual(kwargs["download_kwargs"]["timeout"], (10, 30))
            for blob, filename in pairs:
                Path(filename).write_bytes(b"x")
            return [None] * len(pairs)

        manager.download_many.side_effect = write_batch
        with tempfile.TemporaryDirectory() as directory, patch(
                "hw2.load_transfer_library", return_value=(storage, manager, "test")):
            paths, version = download_with_transfer_manager("public-bucket", items, Path(directory), 4)
            self.assertEqual(len(paths), 130)
            self.assertEqual(version, "test")
            self.assertEqual(manager.download_many.call_count, 5)
            bucket.blob.assert_any_call("pages/129.html", generation=1129)
            self.assertTrue(all(path.read_bytes() == b"x" for path in paths))
        client.close.assert_called_once()

    def test_failed_download_stops_run(self):
        storage, manager, client, _ = self.dependencies()
        manager.download_many.side_effect = RuntimeError("download failed")
        with tempfile.TemporaryDirectory() as directory, patch(
                "hw2.load_transfer_library", return_value=(storage, manager, "test")):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                download_with_transfer_manager("bucket", [{"name": "p/0.html", "generation": "1", "size": "1"}],
                                               Path(directory), 4)
        client.close.assert_called_once()

    def test_missing_or_partial_download_is_rejected(self):
        storage, manager, _, _ = self.dependencies()
        manager.download_many.return_value = [None]
        with tempfile.TemporaryDirectory() as directory, patch(
                "hw2.load_transfer_library", return_value=(storage, manager, "test")):
            with self.assertRaisesRegex(ValueError, "Incomplete download"):
                download_with_transfer_manager("bucket", [{"name": "p/0.html", "generation": "1", "size": "1"}],
                                               Path(directory), 4)

    def test_returned_exception_is_not_silently_accepted(self):
        storage, manager, _, _ = self.dependencies()
        manager.download_many.return_value = [ValueError("failed object")]
        with tempfile.TemporaryDirectory() as directory, patch(
                "hw2.load_transfer_library", return_value=(storage, manager, "test")):
            with self.assertRaisesRegex(RuntimeError, "p/0.html: failed object"):
                download_with_transfer_manager("bucket", [{"name": "p/0.html", "generation": "1", "size": "1"}],
                                               Path(directory), 4)

    def test_retry_keeps_successes_and_replaces_failed_bytes_even_if_size_matches(self):
        storage, manager, client, _ = self.dependencies()
        items = [{"name": f"pages/{i}.html", "generation": str(i + 1), "size": "2"}
                 for i in range(35)]
        attempts, snapshots, stats = {}, [], {}

        def download(pairs, **kwargs):
            results = []
            for (name, generation), filename in pairs:
                self.assertEqual(generation, int(name.split('/')[-1][:-5]) + 1)
                attempts[name] = attempts.get(name, 0) + 1
                if name == "pages/33.html" and attempts[name] == 1:
                    Path(filename).write_bytes(b"xx")
                    results.append(TimeoutError("read stalled"))
                else:
                    Path(filename).write_bytes(b"ok")
                    results.append(None)
            return results

        manager.download_many.side_effect = download
        with tempfile.TemporaryDirectory() as directory, \
             patch("hw2.load_transfer_library", return_value=(storage, manager, "test")), \
             patch("hw2.time.sleep") as sleep, patch("hw2.random.uniform", return_value=0.5):
            paths, _ = download_with_transfer_manager(
                "bucket", items, Path(directory), 8,
                lambda done, total, state: snapshots.append((done, state)), stats)
            self.assertTrue(all(path.read_bytes() == b"ok" for path in paths))
        self.assertEqual(attempts.pop("pages/33.html"), 2)
        self.assertTrue(all(count == 1 for count in attempts.values()))
        self.assertEqual([len(call.args[0]) for call in manager.download_many.call_args_list], [32, 3, 1])
        self.assertEqual(stats["object_download_attempts"], 36)
        self.assertEqual(stats["object_retries"], 1)
        self.assertEqual(stats["completed_objects"], 35)
        self.assertEqual(stats["retry_backoff_seconds"], 0.5)
        self.assertTrue(any(done == 34 and "retrying only 1" in state for done, state in snapshots))
        self.assertEqual(client.close.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_persistent_timeout_has_finite_attempt_budget(self):
        storage, manager, client, _ = self.dependencies()
        manager.download_many.return_value = [TimeoutError("read stalled")]
        stats = {}
        with tempfile.TemporaryDirectory() as directory, \
             patch("hw2.load_transfer_library", return_value=(storage, manager, "test")), \
             patch("hw2.time.sleep") as sleep, patch("hw2.random.uniform", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "exhausted after 5 attempts.*pages/0.html"):
                download_with_transfer_manager("bucket", [
                    {"name": "pages/0.html", "generation": "1", "size": "1"}],
                    Path(directory), 8, diagnostics=stats)
        self.assertEqual(manager.download_many.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(client.close.call_count, 5)
        self.assertEqual(stats["object_download_attempts"], 5)
        self.assertEqual(stats["object_retries"], 4)
        self.assertEqual(stats["completed_objects"], 0)

    def test_disk_errors_are_not_retried(self):
        storage, manager, client, _ = self.dependencies()
        manager.download_many.return_value = [OSError(errno.ENOSPC, "No space left on device")]
        with tempfile.TemporaryDirectory() as directory, \
             patch("hw2.load_transfer_library", return_value=(storage, manager, "test")), \
             patch("hw2.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "No space left"):
                download_with_transfer_manager("bucket", [
                    {"name": "pages/0.html", "generation": "1", "size": "1"}], Path(directory), 8)
        manager.download_many.assert_called_once()
        client.close.assert_called_once()
        sleep.assert_not_called()

    def test_incomplete_manager_result_is_rejected(self):
        storage, manager, _, _ = self.dependencies()
        manager.download_many.return_value = []
        with tempfile.TemporaryDirectory() as directory, patch(
                "hw2.load_transfer_library", return_value=(storage, manager, "test")):
            with self.assertRaisesRegex(RuntimeError, "incomplete batch"):
                download_with_transfer_manager("bucket", [
                    {"name": "pages/0.html", "generation": "1", "size": "1"}], Path(directory), 8)

    def test_google_and_requests_errors_are_classified(self):
        try:
            from google.api_core import exceptions as api_errors
            from requests import exceptions as request_errors
        except ImportError:
            self.skipTest("Install requirements.txt to check Google/Requests error classes")
        timeout = request_errors.ConnectionError("Read timed out")
        for error in (timeout, api_errors.RetryError("Timeout of 120.0s exceeded", timeout),
                      api_errors.TooManyRequests("429"), api_errors.ServiceUnavailable("503")):
            with self.subTest(error=error):
                self.assertTrue(retryable_transfer_error(error))
        for error in (api_errors.Forbidden("403"), api_errors.NotFound("404"),
                      api_errors.PreconditionFailed("412"), ValueError("bug")):
            with self.subTest(error=error):
                self.assertFalse(retryable_transfer_error(error))

    def test_real_google_transfer_manager_with_offline_blob_downloads(self):
        try:
            from google.cloud.storage import Blob
            from requests.exceptions import ConnectionError as RequestConnectionError
        except ImportError:
            self.skipTest("Install requirements.txt to exercise the real Google transfer manager")
        items = [{"name": f"pages/{i}.html", "generation": "7", "size": "2"} for i in range(5)]

        attempts, stats = {}, {}

        def fake_download(blob, file_obj, **kwargs):
            self.assertEqual(blob.generation, 7)
            self.assertIsNone(kwargs["retry"])
            self.assertEqual(kwargs["timeout"], (10, 30))
            self.assertEqual(file_obj.tell(), 0)
            attempts[blob.name] = attempts.get(blob.name, 0) + 1
            if blob.name == "pages/2.html" and attempts[blob.name] == 1:
                file_obj.write(b"partial bytes must not be appended to")
                raise RequestConnectionError("Read timed out")
            file_obj.write(b"ok")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(Blob, "_prep_and_do_download", fake_download), \
             patch("hw2.time.sleep"), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("Offline test attempted network")):
            paths, version = download_with_transfer_manager(
                "unused-test-bucket", items, Path(directory), 2, diagnostics=stats)
            self.assertEqual(len(paths), 5)
            self.assertTrue(version)
            self.assertTrue(all(path.read_bytes() == b"ok" for path in paths))
            self.assertEqual(stats["object_retries"], 1)
            self.assertEqual(attempts["pages/2.html"], 2)


class EndToEndTests(unittest.TestCase):
    def test_sequential_and_transfer_modes_produce_same_graph_results(self):
        documents = [b'<a href="1.html">B</a><a href="2.html">C</a>',
                     b'<a href="2.html">C</a>', b'<a href="0.html">A</a>']
        items = [{"name": f"pages/{i}.html", "generation": str(i+1), "size": str(len(data))}
                 for i, data in enumerate(documents)]
        storage, manager, _, _ = TransferTests().dependencies()
        fail_once = [False]

        def write_batch(pairs, **kwargs):
            results = []
            for blob, filename in pairs:
                index = int(blob[0].split("/")[-1][:-5])
                if fail_once[0] and index == 1:
                    fail_once[0] = False
                    Path(filename).write_bytes(b"partial")
                    results.append(TimeoutError("read stalled"))
                else:
                    Path(filename).write_bytes(documents[index])
                    results.append(None)
            return results

        manager.download_many.side_effect = write_batch
        with tempfile.TemporaryDirectory() as directory:
            summaries = []
            for label in ("sequential", "transfer", "transfer-retry"):
                mode = "sequential" if label == "sequential" else "transfer"
                fail_once[0] = label == "transfer-retry"
                output = Path(directory) / label
                clock = [0.0]
                arguments = ["hw2.py", "--bucket", "test-bucket", "--expected-files", "3",
                             "--environment", "smoke", "--output-dir", str(output),
                             "--download-mode", mode, "--workers", "4"]
                with patch("sys.argv", arguments), patch("sys.stdout", new_callable=io.StringIO), \
                     patch.object(PublicGCS, "list_objects", return_value=items), \
                     patch.object(PublicGCS, "download", side_effect=documents), \
                     patch("hw2.load_transfer_library", return_value=(storage, manager, "test")), \
                     patch("hw2.random.uniform", return_value=0.5), \
                     patch("hw2.time.perf_counter", side_effect=lambda: clock[0]), \
                     patch("hw2.time.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
                    main()
                summary = json.loads((output / "summary.json").read_text())
                summaries.append(summary)
                report = (output / "results.txt").read_text()
                self.assertIn("Top 5 PageRank pages (handout stop", report)
                self.assertIn("P100", report)
                self.assertNotIn("top5_stabilized", report)
                self.assertEqual(summary["runtime"]["algorithm_threads"], 1)
                self.assertFalse(list(output.glob(".downloads-*")))
                self.assertEqual(len((output / "nodes.csv").read_text().splitlines()), 4)
            self.assertEqual(summaries[0]["dataset"]["content_sha256"], summaries[1]["dataset"]["content_sha256"])
            self.assertEqual(summaries[0]["link_statistics"], summaries[1]["link_statistics"])
            self.assertEqual(summaries[0]["closeness"], summaries[1]["closeness"])
            for key in ("top5_handout", "top5_stabilized"):
                self.assertEqual(summaries[0]["pagerank"][key], summaries[1]["pagerank"][key])
            self.assertIsNone(summaries[1]["dataset"]["http_retries"])
            for key in ("link_statistics", "closeness", "dataset"):
                self.assertEqual(summaries[1][key], summaries[2][key])
            for key in ("top5_handout", "top5_stabilized"):
                self.assertEqual(summaries[1]["pagerank"][key], summaries[2]["pagerank"][key])
            self.assertEqual(summaries[2]["source"]["transfer_diagnostics"]["object_retries"], 1)
            self.assertEqual(summaries[2]["timings"]["download_seconds"], 0.5)
            self.assertEqual(summaries[2]["timings"]["total_seconds"], 0.5)

    def test_transfer_failure_cleans_staging_and_does_not_publish_results(self):
        storage, manager, _, _ = TransferTests().dependencies()

        def fail(pairs, **kwargs):
            Path(pairs[0][1]).write_bytes(b"partial")
            return [ValueError("bad download")]

        manager.download_many.side_effect = fail
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "failure"
            arguments = ["hw2.py", "--bucket", "test-bucket", "--expected-files", "1",
                         "--environment", "smoke", "--output-dir", str(output), "--download-mode", "transfer"]
            with patch("sys.argv", arguments), patch("sys.stdout", new_callable=io.StringIO), \
                 patch.object(PublicGCS, "list_objects", return_value=[
                     {"name": "pages/0.html", "generation": "1", "size": "1"}]), \
                 patch("hw2.load_transfer_library", return_value=(storage, manager, "test")), \
                 self.assertRaisesRegex(RuntimeError, "bad download"):
                main()
            self.assertFalse(list(output.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
