#!/usr/bin/env python3
"""CS528 HW2: public GCS reads and single-threaded graph algorithms, Python 3.10+.

Optional concurrent downloads use Google's transfer manager, as allowed on Piazza.
Graph processing uses only the standard library. See README.md for conventions.
"""
import argparse
from array import array
import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import http.client
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import sys
import time
import tempfile
from contextlib import ExitStack
from urllib.parse import quote, urlencode

VERSION = "2.1"
TRANSFER_BATCH_SIZE = 32
TRANSFER_MAX_ATTEMPTS = 5
TRANSFER_TIMEOUT = (10, 30)  # Connection timeout, then read-inactivity timeout.


def load_transfer_library():
    try:
        from google.cloud import storage
        from google.cloud.storage import transfer_manager
        from importlib.metadata import version
    except ImportError as exc:
        raise RuntimeError("Transfer mode requires: python -m pip install -r requirements.txt") from exc
    return storage, transfer_manager, version("google-cloud-storage")


def retryable_transfer_error(error):
    """Retry network failures and transient service errors, not permissions or disk errors."""
    if isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
        return True
    try:
        from google.api_core import exceptions as api_errors
        from google.cloud.storage.exceptions import DataCorruption
        from requests import exceptions as request_errors
    except ImportError:
        return False
    if isinstance(error, api_errors.RetryError):
        return error.cause is not None and retryable_transfer_error(error.cause)
    return isinstance(error, (
        request_errors.ConnectionError, request_errors.Timeout,
        request_errors.ChunkedEncodingError, DataCorruption,
        api_errors.TooManyRequests, api_errors.InternalServerError,
        api_errors.BadGateway, api_errors.ServiceUnavailable, api_errors.GatewayTimeout))


def download_with_transfer_manager(bucket_name, items, directory, workers, progress=None,
                                   diagnostics=None):
    """Download only. All workers finish before the caller parses any HTML.

    Use Google's built-in THREAD workers for these small files. Each Blob retains
    the listed generation. Batches bound queued work and provide progress output.
    No custom thread pool, callbacks, graph work, or persistent cache is used.
    """
    storage, manager, library_version = load_transfer_library()
    stats = diagnostics if diagnostics is not None else {}
    stats.update(batch_size=TRANSFER_BATCH_SIZE, max_attempts_per_object=TRANSFER_MAX_ATTEMPTS,
                 connect_timeout_seconds=TRANSFER_TIMEOUT[0], read_timeout_seconds=TRANSFER_TIMEOUT[1],
                 sdk_retries_enabled=False, object_download_attempts=0, object_retries=0,
                 completed_objects=0, retry_backoff_seconds=0.0)
    paths = [directory / f"{i}.html" for i in range(len(items))]
    client = storage.Client.create_anonymous_client()
    try:
        bucket = client.bucket(bucket_name)
        for start in range(0, len(items), TRANSFER_BATCH_SIZE):
            pending = list(range(start, min(start + TRANSFER_BATCH_SIZE, len(items))))
            for attempt in range(1, TRANSFER_MAX_ATTEMPTS + 1):
                pairs = [(bucket.blob(items[i]["name"], generation=int(items[i]["generation"])),
                          str(paths[i])) for i in pending]
                if progress:
                    progress(stats["completed_objects"], len(items),
                             f"starting batch: {len(pending)} objects, attempt {attempt}/{TRANSFER_MAX_ATTEMPTS}")
                stats["object_download_attempts"] += len(pending)
                if attempt > 1:
                    stats["object_retries"] += len(pending)
                results = manager.download_many(
                    pairs, worker_type=manager.THREAD, max_workers=workers,
                    download_kwargs={"timeout": TRANSFER_TIMEOUT, "retry": None},
                    raise_exception=False, skip_if_exists=False)
                if len(results) != len(pairs):
                    raise RuntimeError("Transfer manager returned an incomplete batch")
                failed = []
                for index, result in zip(pending, results):
                    if isinstance(result, BaseException):
                        if not retryable_transfer_error(result):
                            raise RuntimeError(f"Download failed for {items[index]['name']}: {result}") from result
                        failed.append(index)
                        last_error = result
                    else:
                        path = paths[index]
                        if not path.is_file() or path.stat().st_size != int(items[index]["size"]):
                            raise ValueError(f"Incomplete download: {items[index]['name']}")
                        stats["completed_objects"] += 1
                if progress:
                    progress(stats["completed_objects"], len(items),
                             f"completed batch; {len(failed)} transient failures" if failed else "completed batch")
                if not failed:
                    break
                if attempt == TRANSFER_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Download retries exhausted after {attempt} attempts: {len(failed)} objects; "
                        f"example {items[failed[0]]['name']}; last error: {last_error}") from last_error
                delay = random.uniform(0, min(2 ** (attempt - 1), 8))
                stats["retry_backoff_seconds"] += delay
                if progress:
                    progress(stats["completed_objects"], len(items),
                             f"retrying only {len(failed)} failed objects in {delay:.1f}s; "
                             f"example {items[failed[0]]['name']}; {type(last_error).__name__}: {last_error}")
                time.sleep(delay)
                # The pool has finished. Refresh its HTTP session before retrying
                # failed files; successful files are never resubmitted in this run.
                client.close()
                client = storage.Client.create_anonymous_client()
                bucket = client.bucket(bucket_name)
                pending = failed
    finally:
        client.close()
    return paths, library_version


class PublicGCS:
    """One synchronous HTTPS connection; no credentials, workers, or local cache."""

    def __init__(self, bucket):
        self.bucket = quote(bucket, safe="")
        self.connection = None
        self.requests = 0
        self.retries = 0

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def get(self, path):
        for attempt in range(5):
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPSConnection(
                        "storage.googleapis.com", timeout=90)
                self.requests += 1
                self.connection.request("GET", path, headers={
                    "User-Agent": "cs528-hw2/1.0", "Accept-Encoding": "identity"})
                response = self.connection.getresponse()
                data = response.read()
                status = response.status
                if status == 200:
                    return data
                if status not in (429, 500, 502, 503, 504):
                    raise RuntimeError(
                        f"GCS HTTP {status}: {data[:500].decode('utf-8', 'replace')}\n"
                        "Check the bucket, prefix, and anonymous list/read permissions.")
                error = f"HTTP {status}"
            except (OSError, http.client.HTTPException) as exc:
                error = str(exc)
            self.close()
            if attempt == 4:
                raise RuntimeError(f"GCS request failed after 5 attempts: {error}")
            self.retries += 1
            print(f"Retry {attempt + 1}: {error}", file=sys.stderr, flush=True)
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def list_objects(self, prefix):
        result, token = [], None
        while True:
            params = {"prefix": prefix, "maxResults": 1000,
                      "fields": "items(name,generation,size),nextPageToken"}
            if token:
                params["pageToken"] = token
            page = json.loads(self.get(
                f"/storage/v1/b/{self.bucket}/o?{urlencode(params)}"))
            result.extend(page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                return result

    def download(self, item):
        # Pin the listed generation so mid-run replacements cannot silently mix data.
        path = f"/download/storage/v1/b/{self.bucket}/o/{quote(item['name'], safe='')}"
        return self.get(path + "?" + urlencode({
            "alt": "media", "generation": item["generation"]}))


class LinkParser(HTMLParser):
    def __init__(self, node_count):
        super().__init__(convert_charrefs=True)
        self.node_count = node_count
        self.targets = array("I")

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        match = re.fullmatch(r"(0|[1-9][0-9]*)\.html", href)
        if not match or int(match.group(1)) >= self.node_count:
            raise ValueError(f"Unexpected or missing link target: {href!r}")
        # Repeated anchors and self-links are retained as actual link occurrences.
        self.targets.append(int(match.group(1)))


def parse_links(data, node_count):
    parser = LinkParser(node_count)
    parser.feed(data.decode("utf-8"))
    parser.close()
    return parser.targets


def validate_objects(items, prefix, expected):
    # Ignore a possible zero-byte folder marker, but reject other unexpected objects.
    files = [item for item in items if item["name"] != prefix]
    expected_names = {f"{prefix}{i}.html" for i in range(expected)}
    actual_names = {item["name"] for item in files}
    if actual_names != expected_names or len(files) != expected:
        missing = sorted(expected_names - actual_names)[:5]
        extra = sorted(actual_names - expected_names)[:5]
        raise ValueError(f"Expected exactly {expected} numbered HTML files. "
                         f"Found {len(files)}; missing examples={missing}; extra={extra}")
    return sorted(files, key=lambda item: int(item["name"][len(prefix):-5]))


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot summarize an empty sequence")

    def quantile(p):
        position = (len(ordered) - 1) * p
        lo, hi = math.floor(position), math.ceil(position)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)

    return {"count": len(values), "total": sum(values),
            "mean": sum(values) / len(values), "median": quantile(0.5),
            "min": ordered[0], "max": ordered[-1],
            "quintiles": {
                f"p{p}": quantile(p / 100) for p in (20, 40, 60, 80, 100)},
            "quintile_interpretation": "five upper endpoints P20/P40/P60/P80/P100; "
                                       "explicit interpretation of Piazza's five values",
            "quantile_method": "linear interpolation at (n-1)*p (type 7)"}


def pagerank_step(adjacency, previous, damping=0.85):
    n = len(adjacency)
    current = [(1.0 - damping) / n] * n
    for source, targets in enumerate(adjacency):
        if targets:
            contribution = damping * previous[source] / len(targets)
            for target in targets:
                current[target] += contribution
    return current


def pagerank(adjacency, sum_tolerance=0.005, l1_tolerance=1e-10,
             max_iterations=1000):
    """Save the first handout stop, then continue the SAME recurrence to L1 stability.

    A dangling node has no outgoing contributions. No implicit redistribution or
    normalization is added to the handout formula; consequently mass can be lost.
    """
    if not adjacency:
        raise ValueError("PageRank needs at least one node")
    started = time.perf_counter()
    scores = [1.0 / len(adjacency)] * len(adjacency)
    handout_scores, handout_info = None, None
    history = []
    for iteration in range(1, max_iterations + 1):
        updated = pagerank_step(adjacency, scores)
        old_sum, new_sum = math.fsum(scores), math.fsum(updated)
        sum_change = abs(new_sum - old_sum) / abs(old_sum)
        l1_change = math.fsum(abs(a - b) for a, b in zip(updated, scores))
        row = {"iteration": iteration, "sum": new_sum,
               "relative_sum_change": sum_change, "l1_change": l1_change}
        history.append(row)
        scores = updated
        if handout_scores is None and sum_change <= sum_tolerance:
            handout_scores = scores.copy()
            handout_info = dict(row, seconds=time.perf_counter() - started)
        if sum_change <= sum_tolerance and l1_change <= l1_tolerance:
            residual = math.fsum(abs(a - b) for a, b in zip(
                pagerank_step(adjacency, scores), scores))
            return handout_scores, scores, {
                "handout_stop": handout_info,
                "stabilized_stop": dict(row, fixed_point_l1_residual=residual),
                "history": history,
                "damping": 0.85, "initial_score": 1.0 / len(adjacency),
                "relative_sum_tolerance": sum_tolerance,
                "l1_tolerance": l1_tolerance,
                "dangling_policy": "no outgoing contribution; literal handout formula",
            }
    raise RuntimeError(f"PageRank did not converge within {max_iterations} iterations")


def adjacency_bits(adjacency):
    result = []
    for targets in adjacency:
        row = 0
        for target in targets:
            row |= 1 << target
        result.append(row)
    return result


def distance_summary(rows, source):
    """Exact outward, unweighted BFS using Python integer sets; no parallelism.

    At each depth, union outgoing neighbors of the frontier. Once that union
    includes every remaining vertex, additional unions cannot change the layer.
    """
    all_nodes = (1 << len(rows)) - 1
    visited = 1 << source
    frontier = visited
    depth = 0
    distance_sum = 0
    while frontier:
        next_nodes = visited
        while frontier:
            bit = frontier & -frontier
            frontier ^= bit
            next_nodes |= rows[bit.bit_length() - 1]
            if next_nodes == all_nodes:
                break
        frontier = next_nodes & ~visited
        depth += 1
        distance_sum += depth * frontier.bit_count()
        visited = next_nodes
        if visited == all_nodes:
            break
    return visited.bit_count() - 1, distance_sum


def closeness(adjacency, progress=None):
    rows = adjacency_bits(adjacency)
    n = len(rows)
    result = []
    for source in range(n):
        reachable, distance_sum = distance_summary(rows, source)
        # Primary score follows (n-1)/sum(d); infinite distance gives score zero.
        strict = ((n - 1) / distance_sum
                  if n > 1 and reachable == n - 1 and distance_sum else 0.0)
        # Supplement for disconnected graphs, clearly separated from primary score.
        wf = (reachable * reachable / ((n - 1) * distance_sum)
              if n > 1 and distance_sum else 0.0)
        result.append({"node": source, "score": strict, "wf_score": wf,
                       "reachable_other_nodes": reachable,
                       "sum_finite_distances": distance_sum})
        if progress and ((source + 1) % 500 == 0 or source + 1 == n):
            progress(source + 1, n)
    return result


def ranked(scores):
    return [{"page": f"{i}.html", "score": scores[i]}
            for i in sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:5]]


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def format_results(summary):
    """One clearly identified primary result for each requested metric."""
    lines = [f"CS528 HW2 results | {summary['environment']} | version {VERSION}",
             f"Files: {summary['dataset']['files']} | Links: {summary['dataset']['total_links']}",
             "", "Link statistics (five quintile upper endpoints):",
             "Direction       Mean     Median        Max        Min        P20        P40        P60        P80       P100"]
    for direction in ("incoming", "outgoing"):
        stats = summary["link_statistics"][direction]
        values = [stats[k] for k in ("mean", "median", "max", "min")]
        values.extend(stats["quintiles"].values())
        lines.append(f"{direction:<10}" + "".join(f" {value:10.4f}" for value in values))
    pr = summary["pagerank"]
    lines.extend(["", f"Top 5 PageRank pages (handout stop, iteration {pr['handout_stop']['iteration']}):"])
    lines.extend(f"  {rank}. {row['page']:<12} {row['score']:.12g}"
                 for rank, row in enumerate(pr["top5_handout"], 1))
    winner = summary["closeness"]["winners"][0]
    lines.extend(["", f"Best outward closeness: {winner['page']} = {winner['score']:.12g}",
                  f"  Reachable other pages: {winner['reachable_other_nodes']}; "
                  f"sum of finite distances: {winner['sum_finite_distances']}"])
    if len(summary["closeness"]["winners"]) > 1:
        lines.append(f"  {len(summary['closeness']['winners'])} tied winners; all saved in summary.json.")
    lines.extend(["", "Phase wall times (seconds):"])
    for key in ("list_seconds", "download_seconds", "local_read_seconds",
                "parse_graph_hash_seconds", "statistics_seconds", "pagerank_total_seconds",
                "closeness_seconds", "csv_write_seconds", "total_seconds"):
        lines.append(f"  {key:<28} {summary['timings'][key]:.6f}")
    lines.extend([f"  PageRank handout/extra: {summary['timings']['pagerank_handout_seconds']:.6f} / "
                  f"{summary['timings']['pagerank_extra_seconds']:.6f} (included above)",
                  f"Dataset SHA-256: {summary['dataset']['content_sha256']}",
                  f"Download mode: {summary['source']['download_mode']}; "
                  f"workers: {summary['source']['download_workers']}; graph threads: 1",
                  "Supplementary stabilized PageRank and convergence diagnostics: summary.json and pagerank_iterations.csv."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"CS528 HW2 {VERSION}")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bucket", help="Public GCS bucket name, without gs://")
    source.add_argument("--local-dir", type=Path, help="Local HTML directory; smoke tests only")
    parser.add_argument("--prefix", default="pages/", help="GCS object prefix (default: pages/)")
    parser.add_argument("--expected-files", type=int, default=12000)
    parser.add_argument("--download-mode", choices=("sequential", "transfer"), default="sequential",
                        help="transfer uses Google's concurrent downloader; graph work stays single-threaded")
    parser.add_argument("--workers", type=int, default=4, help="Transfer-manager workers (default: 4)")
    parser.add_argument("--environment", required=True, help="laptop, cloudshell, vm, or smoke")
    parser.add_argument("--output-dir", required=True, type=Path, help="New directory for this run")
    parser.add_argument("--sum-tolerance", type=float, default=0.005,
                        help="Relative total-score change; 0.005 means 0.5%%")
    parser.add_argument("--l1-tolerance", type=float, default=1e-10,
                        help="Extra check on sum of absolute per-node score changes")
    parser.add_argument("--max-iterations", type=int, default=1000)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")
    if args.expected_files < 1 or args.max_iterations < 1:
        parser.error("File and iteration counts must be positive")
    if args.workers < 1:
        parser.error("Worker count must be positive")
    if args.local_dir and args.download_mode != "sequential":
        parser.error("Transfer mode requires --bucket")
    if args.download_mode == "transfer":
        load_transfer_library()  # Fail before creating output if dependency is missing.
    if not 0 < args.sum_tolerance < 1 or not 0 < args.l1_tolerance < 1:
        parser.error("Tolerances must be strictly between 0 and 1")
    if args.bucket and ("/" in args.bucket or ":" in args.bucket):
        parser.error("Use the bucket name without gs:// or a folder")
    if args.prefix and not args.prefix.endswith("/"):
        args.prefix += "/"
    if args.output_dir.exists():
        parser.error("Output directory already exists; choose a new run name")

    args.output_dir.mkdir(parents=True)
    total_start, cpu_start = time.perf_counter(), time.process_time()
    started_at = datetime.now(timezone.utc).isoformat()
    timings = {}
    client = PublicGCS(args.bucket) if args.bucket else None
    transfer_version = None
    transfer_diagnostics = None
    prefix = args.prefix if client else ""
    with ExitStack() as stack:
        if client:
            stack.callback(client.close)
        before = time.perf_counter()
        if client:
            items = client.list_objects(prefix)
        else:
            items = [{"name": p.name, "size": p.stat().st_size}
                     for p in args.local_dir.iterdir() if p.is_file()]
        items = validate_objects(items, prefix, args.expected_files)
        timings["list_seconds"] = time.perf_counter() - before
        print(f"Listed and verified {len(items)} HTML files.", flush=True)

        adjacency, indegree = [], [0] * args.expected_files
        content_hash = hashlib.sha256()
        download_seconds = parse_seconds = local_read_seconds = 0.0
        paths = None
        if args.download_mode == "transfer":
            # This newly created temporary directory belongs to this run only.
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix=".downloads-", dir=args.output_dir)))
            if not directory.resolve().is_relative_to(args.output_dir.resolve()):
                raise RuntimeError("Temporary download directory is outside the run directory")
            before = time.perf_counter()
            transfer_diagnostics = {}
            paths, transfer_version = download_with_transfer_manager(
                args.bucket, items, directory, args.workers,
                lambda done, total, state: print(
                    f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                    f"Download {done}/{total}: {state}; elapsed {time.perf_counter() - before:.1f}s", flush=True),
                diagnostics=transfer_diagnostics)
            download_seconds = time.perf_counter() - before
            print("All downloads finished; starting single-threaded parsing.", flush=True)
        last_progress = time.perf_counter()
        byte_count = self_links = duplicate_links = 0
        for index, item in enumerate(items):
            before = time.perf_counter()
            if paths is not None:
                data = paths[index].read_bytes()
                local_read_seconds += time.perf_counter() - before
            elif client:
                data = client.download(item)
                download_seconds += time.perf_counter() - before
            else:
                data = (args.local_dir / item["name"]).read_bytes()
                local_read_seconds += time.perf_counter() - before
            before = time.perf_counter()
            if len(data) != int(item["size"]):
                raise ValueError(f"Size mismatch: {item['name']}")
            name = f"{index}.html".encode("utf-8")
            content_hash.update(len(name).to_bytes(8, "big"))
            content_hash.update(name)
            content_hash.update(len(data).to_bytes(8, "big"))
            content_hash.update(data)
            byte_count += len(data)
            targets = parse_links(data, args.expected_files)
            adjacency.append(targets)
            for target in targets:
                indegree[target] += 1
            self_links += targets.count(index)
            duplicate_links += len(targets) - len(set(targets))
            parse_seconds += time.perf_counter() - before
            if ((index + 1) % 500 == 0 or index + 1 == len(items)
                    or time.perf_counter() - last_progress >= 15):
                print(f"Read and parsed {index + 1}/{len(items)} files.", flush=True)
                last_progress = time.perf_counter()
        timings["download_seconds"] = download_seconds
        timings["local_read_seconds"] = local_read_seconds
        timings["parse_graph_hash_seconds"] = parse_seconds

    before = time.perf_counter()
    outdegree = [len(targets) for targets in adjacency]
    statistics = {"incoming": distribution(indegree), "outgoing": distribution(outdegree)}
    if sum(indegree) != sum(outdegree):
        raise AssertionError("Incoming and outgoing link totals must agree")
    timings["statistics_seconds"] = time.perf_counter() - before

    print("Computing PageRank and the extra convergence diagnostic...", flush=True)
    before = time.perf_counter()
    handout, stable, pr_info = pagerank(adjacency, args.sum_tolerance,
                                      args.l1_tolerance, args.max_iterations)
    timings["pagerank_total_seconds"] = time.perf_counter() - before
    timings["pagerank_handout_seconds"] = pr_info["handout_stop"]["seconds"]
    timings["pagerank_extra_seconds"] = (
        timings["pagerank_total_seconds"] - timings["pagerank_handout_seconds"])

    print("Computing exact outward closeness for every page...", flush=True)
    before = time.perf_counter()
    centrality = closeness(adjacency, lambda done, n: print(
        f"Closeness {done}/{n}", flush=True))
    timings["closeness_seconds"] = time.perf_counter() - before
    best_score = max(row["score"] for row in centrality)
    winners = [dict(row, page=f"{row['node']}.html")
               for row in centrality if row["score"] == best_score]

    before = time.perf_counter()
    rows = [{"page": f"{i}.html", "incoming_links": indegree[i],
             "outgoing_links": outdegree[i], "pagerank_handout": handout[i],
             "pagerank_stabilized": stable[i],
             "closeness_outward": centrality[i]["score"],
             "closeness_wf": centrality[i]["wf_score"],
             "reachable_other_nodes": centrality[i]["reachable_other_nodes"],
             "sum_finite_distances": centrality[i]["sum_finite_distances"]}
            for i in range(args.expected_files)]
    write_csv(args.output_dir / "nodes.csv", rows)
    write_csv(args.output_dir / "pagerank_iterations.csv", pr_info.pop("history"))
    timings["csv_write_seconds"] = time.perf_counter() - before
    timings["total_seconds"] = time.perf_counter() - total_start
    timings["process_cpu_seconds"] = time.process_time() - cpu_start

    summary = {
        "schema_version": 2, "program_version": VERSION,
        "environment": args.environment, "started_utc": started_at,
        "source": {"bucket": args.bucket, "prefix": prefix,
                   "local_dir": str(args.local_dir) if args.local_dir else None,
                   "authentication": "anonymous" if client else "local",
                   "download_mode": args.download_mode if client else "local",
                   "download_workers": args.workers if args.download_mode == "transfer" else 1,
                   "google_cloud_storage_version": transfer_version,
                   "transfer_diagnostics": transfer_diagnostics},
        "runtime": {"python": sys.version, "os": platform.platform(),
                    "machine": platform.machine(), "processor": platform.processor(),
                    "logical_cpu_count": os.cpu_count(), "algorithm_threads": 1,
                    "program_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "dataset": {"files": len(adjacency), "bytes": byte_count,
                    "content_sha256": content_hash.hexdigest(),
                    "total_links": sum(outdegree), "self_links": self_links,
                    "repeated_links_beyond_first": duplicate_links,
                    "dangling_pages": outdegree.count(0),
                    "http_requests": (client.requests if args.download_mode == "sequential" and client
                                      else (None if client else 0)),
                    "http_retries": (client.retries if args.download_mode == "sequential" and client
                                     else (None if client else 0)),
                    "http_counters_note": "Transfer-mode HTTP counts are unavailable. "
                                          "Object-level attempts/retries are in source.transfer_diagnostics."},
        "link_statistics": statistics,
        "pagerank": dict(pr_info, top5_handout=ranked(handout),
                         top5_stabilized=ranked(stable)),
        "closeness": {"direction": "outgoing directed shortest paths",
                      "formula": "(n-1)/sum distances; zero if any other node unreachable",
                      "supplementary_wf_formula": "r^2/((n-1)*sum finite distances)",
                      "best_score": best_score, "winners": winners},
        "timings": timings,
        "timing_note": "Total includes CSV output and supplementary PR convergence, "
                       "but excludes startup/imports and final JSON/text/console summary. "
                       "PageRank handout/extra are components of PageRank total.",
    }
    text = json.dumps(summary, indent=2, allow_nan=False)
    (args.output_dir / "summary.json").write_text(text + "\n", encoding="utf-8")
    report = format_results(summary)
    (args.output_dir / "results.txt").write_text(report, encoding="utf-8")
    print(report, flush=True)
    print(f"Saved results in {args.output_dir.resolve()}", flush=True)


if __name__ == "__main__":
    main()
