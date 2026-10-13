import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stress_worker import ResponseParser, Sampler
from plot_stress import summarize
import run_stress


class StressTests(unittest.TestCase):
    def test_verbose_client_responses_and_planned_cutoff(self):
        parser = ResponseParser()
        result = None
        for line in ['Requesting  /pages/0.html  from  10.0.0.1 8080', '200 OK',
                     'Content-Length: 20', 'X-Request-Id: example-id', "b'<!DOCTYPE html>abc'"]:
            result = parser.feed(line)
        self.assertEqual(result['status'], 200)
        self.assertTrue(result['body_valid'])
        self.assertEqual(result['response_bytes'], 20)
        parser.feed('Requesting /pages/1.html from 10.0.0.1 8080')
        self.assertEqual(parser.finish(True)['kind'], 'boundary_interrupted')
        parser.feed('Requesting /pages/2.html from 10.0.0.1 8080')
        self.assertEqual(parser.finish(False)['kind'], 'transport_failure')

    def test_parser_matches_actual_saved_provided_client_format(self):
        path = ROOT / 'evidence/client-100-20261013T074028911477Z/client.stdout.txt'
        if not path.exists():
            self.skipTest('Local demonstration evidence is not part of the submitted repository.')
        parser, responses = ResponseParser(), []
        with path.open() as stream:
            for line in stream:
                response = parser.feed(line.rstrip('\r\n'))
                if response:
                    responses.append(response)
        self.assertEqual(len(responses), 100)
        self.assertEqual(sum(r['status'] == 400 for r in responses), 6)
        self.assertTrue(all(r['body_valid'] and r['event_id'] for r in responses))

    def test_timeout_is_recorded_and_next_request_can_complete(self):
        parser = ResponseParser()
        parser.feed('Requesting  /pages/2465.html  from  104.197.255.22 8080')
        failure = parser.feed('Timeout received')
        self.assertEqual(failure['kind'], 'transport_failure')
        self.assertEqual(failure['reason'], 'client_timeout')
        self.assertEqual(failure['filename'], '/pages/2465.html')
        self.assertIsNone(parser.feed('Requesting  /pages/2626.html  from  104.197.255.22 8080'))
        for line in ['200 OK', 'Content-Length: 20', 'X-Request-Id: next-id', "b'<!DOCTYPE html>abc'"]:
            response = parser.feed(line)
        self.assertEqual(response['kind'], 'response')
        self.assertEqual(response['filename'], '/pages/2626.html')
        self.assertTrue(response['body_valid'])
        self.assertIsNone(parser.finish(True))

    def test_missing_response_resynchronizes_without_erasing_failure(self):
        parser = ResponseParser()
        parser.feed('Requesting /pages/first.html from 10.0.0.1 8080')
        failure = parser.feed('Requesting /pages/second.html from 10.0.0.1 8080')
        self.assertEqual(failure['kind'], 'transport_failure')
        self.assertEqual(failure['reason'], 'missing_response_before_next_request')
        self.assertEqual(failure['filename'], '/pages/first.html')
        self.assertEqual(parser.finish(True)['filename'], '/pages/second.html')

    def test_proc_counter_fixture_excludes_duplicate_guest_ticks(self):
        cpu = iter(['cpu 100 0 40 800 20 0 10 30 90 0\n',
                    'cpu 160 0 60 870 30 0 15 40 140 0\n'])
        nic = iter(['eth0: 100000 0 0 0 0 0 0 0 200000 0 0 0 0 0 0 0',
                    'eth0: 350000 0 0 0 0 0 0 0 700000 0 0 0 0 0 0 0'])

        def read(path, *args, **kwargs):
            name = str(path).replace('\\', '/')
            if name == '/proc/net/route':
                return 'Iface Destination\neth0 00000000\n'
            if name == '/proc/stat':
                return next(cpu)
            if name == '/proc/meminfo':
                return 'MemTotal: 512000 kB\nMemAvailable: 128000 kB\n'
            if name == '/proc/net/dev':
                return 'header\nheader\n' + next(nic) + '\n'
            raise AssertionError(name)

        with patch.object(Path, 'read_text', read), patch('stress_worker.time.time', side_effect=[100, 101]), \
                patch('stress_worker.time.monotonic', side_effect=[10, 11]):
            sampler = Sampler()
            self.assertIsNone(sampler.read()['cpu_pct'])
            row = sampler.read()
        self.assertAlmostEqual(row['cpu_pct'], 85 / 175 * 100)
        self.assertEqual((row['rx_mbps'], row['tx_mbps']), (2, 4))
        self.assertEqual(row['memory_used_mib'], 375)

    def fixture(self, directory, status=400, coverage=True):
        client, server = directory / 'client', directory / 'server'
        client.mkdir(); server.mkdir()
        run = dict(status='complete', seconds=10, counts=[1], stages=[dict(clients=1,
                   startEpoch=0, endEpoch=10, startedAt='example')])
        (client / 'run.json').write_text(json.dumps(run))
        rows = []
        for i in range(1, 11 if coverage else 3):
            rows.append(dict(epoch=i, interval_start=i-1, active_clients=1, cpu_pct=20,
                             steal_pct=0, iowait_pct=0, memory_used_mib=100, server_rss_mib=60,
                             server_cpu_pct=10, rx_mbps=1, tx_mbps=2))
        for p in (client / 'client-metrics.jsonl', server / 'server-metrics.jsonl'):
            p.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        records = [dict(kind='response', status=status, event_id='id', body_valid=True, response_bytes=19),
                   dict(kind='boundary_interrupted'), dict(kind='process_exit', planned=True)]
        (client / 'clients-1-slot-1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))

    def test_expected_400_and_cutoff_are_excluded_from_capacity_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory)
            result = summarize(directory)
        self.assertTrue(result['measurement_valid'])
        self.assertFalse(result['report_ready'])
        self.assertIsNone(result['first_failure_clients'])
        self.assertEqual(result['stages'][0]['expected_denials'], 1)
        self.assertEqual(result['stages'][0]['boundary_interrupted'], 1)

    def test_unexpected_503_is_a_failure_signal(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory, status=503)
            result = summarize(directory)
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(result['first_failure_clients'], 1)
        self.assertEqual(result['stages'][0]['unexpected_http'], 1)

    def test_timeout_keeps_valid_measurement_but_is_a_failure_signal(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory)
            with (directory / 'client/clients-1-slot-1.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(kind='transport_failure', reason='client_timeout')) + '\n')
            result = summarize(directory)
        self.assertTrue(result['measurement_valid'])
        self.assertEqual(result['first_failure_clients'], 1)
        self.assertEqual(result['stages'][0]['request_timeouts'], 1)
        self.assertEqual(result['stages'][0]['transport_failures'], 1)
        self.assertTrue(result['stages'][0]['failure_observed'])

    def test_partial_summary_does_not_claim_untested_load_levels(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory)
            path = directory / 'client/run.json'
            run = json.loads(path.read_text())
            run.update(status='failed', counts=[1, 2, 3, 4], error='parser failed')
            run['stages'].append(dict(clients=2, startEpoch=12, startedAt='example'))
            path.write_text(json.dumps(run))
            result = summarize(directory)
            text = (directory / 'results.md').read_text()
        self.assertFalse(result['measurement_valid'])
        self.assertEqual(result['incomplete_load_levels'], [2])
        self.assertEqual(result['run_error'], 'parser failed')
        self.assertIn('up to 1 clients', text)
        self.assertNotIn('up to 4 clients', text)
        self.assertIn('Worker error: parser failed', text)

    def test_short_sampling_coverage_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory, coverage=False)
            self.assertFalse(summarize(directory)['measurement_valid'])

    def test_fifth_client_refused_before_cloud_access(self):
        with patch.object(sys, 'argv', ['run_stress.py', '--counts', '5']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                run_stress.main()
        self.assertEqual(raised.exception.code, 2)

    def test_crashed_status_read_is_retried_before_accepting_completion(self):
        attempts = [SimpleNamespace(returncode=3221225477, stdout='{"status":"complete"}'),
                    SimpleNamespace(returncode=0, stdout='{"status":"complete"}')]
        with patch('run_stress.ssh', side_effect=attempts) as command, patch('run_stress.time.sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            status = run_stress.poll_status(None, {}, '/tmp/test')
        self.assertEqual(command.call_count, 2)
        self.assertEqual(status['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
