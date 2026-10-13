"""Linux-only, standard-library sampler and bounded provided-client workloads."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def append(stream, value):
    stream.write(json.dumps(value) + '\n')
    stream.flush()


def default_interface():
    for line in Path('/proc/net/route').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == '00000000':
            return fields[0]
    raise RuntimeError('Cannot identify the default network interface.')


class Sampler:
    def __init__(self, unit=None):
        self.interface = default_interface()
        self.previous = None
        self.pid = None
        if unit:
            self.pid = int(subprocess.check_output(
                ['systemctl', 'show', unit, '--property=MainPID', '--value'], text=True).strip())
            if not self.pid:
                raise RuntimeError('The server systemd service is not running.')

    def read(self):
        cpu = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, value = line.split(':', 1)
            memory[name] = int(value.split()[0])
        network = None
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            name, counters = line.split(':', 1)
            if name.strip() == self.interface:
                fields = list(map(int, counters.split()))
                network = (fields[0], fields[8])
        if network is None:
            raise RuntimeError('Measured interface disappeared.')
        epoch, mono = time.time(), time.monotonic()
        rss, ticks = None, None
        if self.pid:
            try:
                stat = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()
                ticks = int(stat[11]) + int(stat[12])
                rss = int(stat[21]) * os.sysconf('SC_PAGE_SIZE') / 1048576
            except (OSError, ValueError):
                raise RuntimeError('Server process disappeared or restarted during measurement.')
        row = dict(epoch=epoch, timestamp=utc(), monotonic=mono, interface=self.interface,
                   memory_used_mib=(memory['MemTotal'] - memory['MemAvailable']) / 1024,
                   memory_total_mib=memory['MemTotal'] / 1024, server_rss_mib=rss,
                   cpu_pct=None, steal_pct=None, iowait_pct=None,
                   rx_mbps=None, tx_mbps=None, server_cpu_pct=None, interval_start=None)
        if self.previous:
            old_cpu, old_network, old_mono, old_epoch, old_ticks = self.previous
            delta = [a - b for a, b in zip(cpu, old_cpu)]
            total, elapsed = sum(delta), mono - old_mono
            if total <= 0 or elapsed <= 0:
                raise RuntimeError('Nonpositive measurement interval.')
            # Guest ticks are already counted in user/nice; only the first eight fields are summed.
            row.update(interval_start=old_epoch,
                       cpu_pct=100 * (total - delta[3] - delta[4] - delta[7]) / total,
                       steal_pct=100 * delta[7] / total, iowait_pct=100 * delta[4] / total,
                       rx_mbps=(network[0] - old_network[0]) * 8 / elapsed / 1000000,
                       tx_mbps=(network[1] - old_network[1]) * 8 / elapsed / 1000000)
            if ticks is not None:
                row['server_cpu_pct'] = 100 * (ticks - old_ticks) / os.sysconf('SC_CLK_TCK') / elapsed
        self.previous = (cpu, network, mono, epoch, ticks)
        return row


class ResponseParser:
    """Keep compact response evidence; discard the large verbose HTML body lines."""
    def __init__(self):
        self.current = None

    def feed(self, line):
        request = re.match(r'^Requesting\s+(\S+)\s+from\s+(\S+)\s+(\d+)', line)
        if request:
            # A failed request can be followed by another request without a body.
            # Preserve that outcome and resynchronize instead of aborting the suite.
            unfinished = self.finish(False, reason='missing_response_before_next_request')
            self.current = dict(filename=request[1], status=None, event_id=None,
                                response_bytes=None, body_valid=False)
            return unfinished
        elif self.current is not None:
            if line.strip() == 'Timeout received':
                return self.finish(False, reason='client_timeout', detail=line.strip())
            status = re.match(r'^(\d{3})\s+', line)
            if status:
                self.current['status'] = int(status[1])
            elif line.lower().startswith('x-request-id:'):
                self.current['event_id'] = line.split(':', 1)[1].strip()
            elif line.lower().startswith('content-length:'):
                self.current['response_bytes'] = int(line.split(':', 1)[1].strip())
            elif line.startswith(("b'", 'b"')):
                expected = {200: '<!DOCTYPE html>', 400: 'Permission denied.'}.get(self.current['status'])
                self.current['body_valid'] = expected is None or line[2:].startswith(expected)
                result, self.current = self.current, None
                return dict(kind='response', **result)
        return None

    def finish(self, planned, **diagnostic):
        if self.current is None:
            return None
        result, self.current = self.current, None
        return dict(kind='boundary_interrupted' if planned else 'transport_failure', **result, **diagnostic)


def client_arguments(config, seed):
    return [config.get('binary', '/opt/cs528-hw4/http-client'), '-d', config['serverIp'],
            '-b', 'none', '-w', config['objectPrefix'].strip('/'), '-n', '100000000',
            '-i', str(config['maxFileIndex']), '-p', str(config['port']), '-r', str(seed),
            '-t', '10', '-v']


async def terminate(process):
    # The PyInstaller binary can have a bootloader child; stop the whole owned process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def client_slot(slot, count, deadline, directory, config, active):
    responses_path = directory / f'clients-{count}-slot-{slot}.jsonl'
    with responses_path.open('w') as records, \
            (directory / f'clients-{count}-slot-{slot}.stdout.txt').open('w') as compact, \
            (directory / f'clients-{count}-slot-{slot}.stderr.txt').open('w') as errors:
        for attempt in range(3):
            if time.monotonic() >= deadline or (directory / 'STOP').exists():
                break
            args = client_arguments(config, 528 + slot + attempt * 10000)
            append(records, dict(kind='command', arguments=args, timestamp=utc(), epoch=time.time()))
            env = dict(os.environ, PYTHONUNBUFFERED='1')
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                stderr=errors, env=env, start_new_session=True, limit=4 * 1024 * 1024)
            active[slot] = process
            parser = ResponseParser()

            async def pump():
                while True:
                    data = await process.stdout.readline()
                    if not data:
                        return
                    line = data.decode('utf-8', errors='replace').rstrip('\r\n')
                    if not line.startswith(("b'", 'b"')):
                        compact.write(line[:2000] + '\n')
                    record = parser.feed(line)
                    if record:
                        append(records, dict(**record, epoch=time.time(), timestamp=utc(), slot=slot, clients=count))

            reader = asyncio.create_task(pump())
            planned = False
            try:
                while process.returncode is None:
                    if reader.done() and reader.exception():
                        raise reader.exception()
                    if time.monotonic() >= deadline or (directory / 'STOP').exists():
                        planned = True
                        await terminate(process)
                        break
                    await asyncio.sleep(.2)
                await asyncio.wait_for(reader, 5)
                unfinished = parser.finish(planned)
                if unfinished:
                    append(records, dict(**unfinished, epoch=time.time(), slot=slot, clients=count))
                append(records, dict(kind='process_exit', returncode=process.returncode,
                                     planned=planned, epoch=time.time(), slot=slot, clients=count))
            finally:
                await terminate(process)
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                active.pop(slot, None)
            if planned:
                break
            # An early crash is retained as a failure signal; restart only within the same slot.
            await asyncio.sleep(.5)


async def run_clients(args, config):
    directory = args.directory
    active, stages = {}, []
    done = asyncio.Event()
    sampler = Sampler()

    async def measure_client():
        with (directory / 'client-metrics.jsonl').open('w') as stream:
            while not done.is_set():
                row = sampler.read()
                row['active_clients'] = sum(p.returncode is None for p in active.values())
                append(stream, row)
                await asyncio.sleep(1)

    measurement = asyncio.create_task(measure_client())
    result = dict(status='running', startedAt=utc(), config=config, seconds=args.seconds,
                  counts=args.counts, cooldown=args.cooldown, stages=stages,
                  measurementNotes='Verbose body lines discarded; response timestamps are stdout observations, not latency measurements.')
    write_json(directory / 'status.json', dict(status='baseline', clients=0))
    try:
        # Reject unrelated pre-existing provided-client processes before any workload starts.
        binary = config.get('binary', '/opt/cs528-hw4/http-client').encode()
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                if path.read_bytes().split(b'\0')[0] == binary:
                    raise RuntimeError('An existing http-client is running. Finish it before this suite.')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        await asyncio.sleep(5)
        for count in args.counts:
            if (directory / 'STOP').exists():
                raise RuntimeError('Workload cancelled.')
            if measurement.done():
                measurement.result()
            stage = dict(clients=count, startEpoch=time.time(), startedAt=utc(), plannedSeconds=args.seconds)
            stages.append(stage)
            deadline = time.monotonic() + args.seconds
            write_json(directory / 'status.json', dict(status='running', clients=count, startedAt=stage['startedAt']))
            tasks = [asyncio.create_task(client_slot(i, count, deadline, directory, config, active))
                     for i in range(1, count + 1)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            while time.monotonic() < deadline and not (directory / 'STOP').exists():
                await asyncio.sleep(.2)
            stage.update(endEpoch=stage['startEpoch'] + args.seconds, endedAt=utc())
            write_json(directory / 'run.json', result)
            if (directory / 'STOP').exists():
                raise RuntimeError('Workload cancelled.')
            if count != args.counts[-1]:
                write_json(directory / 'status.json', dict(status='cooldown', clients=count))
                await asyncio.sleep(args.cooldown)
        result['status'] = 'complete'
    except BaseException as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        done.set()
        try:
            await measurement
        except Exception as error:
            result.update(status='failed', error='Client sampler: ' + str(error))
        result['finishedAt'] = utc()
        write_json(directory / 'run.json', result)
        write_json(directory / 'status.json', dict(status=result['status'], error=result.get('error')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['sample', 'run', 'stop'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--counts', type=int, nargs='+', default=[1, 2, 3, 4])
    parser.add_argument('--cooldown', type=int, default=15)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--unit')
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.mode == 'stop':
        (args.directory / 'STOP').touch()
        return
    if args.mode == 'sample':
        sampler = Sampler(args.unit)
        with (args.directory / 'server-metrics.jsonl').open('w') as stream:
            end = time.monotonic() + args.seconds
            while time.monotonic() < end and not (args.directory / 'STOP').exists():
                append(stream, sampler.read())
                time.sleep(1)
        return
    if not 5 <= args.seconds <= 180 or not args.counts or any(n not in range(1, 5) for n in args.counts):
        parser.error('Use 5-180 seconds and 1-4 logical client processes.')
    if args.counts != sorted(set(args.counts)) or not 0 <= args.cooldown <= 60:
        parser.error('Counts must be unique/increasing; cooldown must be 0-60 seconds.')
    import fcntl
    with open('/tmp/cs528-hw4-stress.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_json(args.directory / 'status.json', dict(status='failed', error='Another HW4 stress suite is running.'))
            raise SystemExit('Another HW4 stress suite is running.')
        asyncio.run(run_clients(args, json.loads(args.config.read_text())))


if __name__ == '__main__':
    main()
