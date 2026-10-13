"""Run bounded 1-4 provided-client workloads and export Linux resource samples."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import tarfile
import time

from deploy import ROOT, Gcloud, ssh, owned, save
from plot_stress import summarize


def poll_status(gc, config, remote):
    path = shlex.quote(remote + '/status.json')
    command = 'if test -f ' + path + '; then cat ' + path + "; else printf '%s' '{\"status\":\"starting\"}'; fi"
    for attempt in range(3):
        result = ssh(gc, config, 'client', command, check=False, timeout=60)
        if result.returncode == 0:
            try:
                status = json.loads(result.stdout)
                if isinstance(status, dict) and isinstance(status.get('status'), str):
                    return status
            except ValueError:
                pass
        print(f'Status read failed (exit {result.returncode}); retry {attempt + 1}/3.', flush=True)
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError('Three status reads failed; partial evidence will be exported.')


def upload(gc, config, role, local, destination):
    gc.run(['compute', 'scp', str(local), config['prefix'] + '-' + role + ':' + destination,
            '--zone=' + config['zone'], '--tunnel-through-iap'])


def download(gc, config, role, remote, folder):
    archive = remote + '.tar.gz'
    ssh(gc, config, role, 'tar -czf ' + shlex.quote(archive) + ' -C ' + shlex.quote(remote) + ' .')
    local = folder / (role + '.tar.gz')
    gc.run(['compute', 'scp', config['prefix'] + '-' + role + ':' + archive, str(local),
            '--zone=' + config['zone'], '--tunnel-through-iap'])
    destination = folder / role
    destination.mkdir(exist_ok=True)
    with tarfile.open(local) as stream:
        # These archives contain only our run files; also reject unsafe archive paths/links.
        stream.extractall(destination, filter='data')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--counts', type=int, nargs='+', default=[1, 2, 3, 4])
    parser.add_argument('--cooldown', type=int, default=15)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 180 or args.counts != sorted(set(args.counts)) \
            or any(n not in range(1, 5) for n in args.counts) or not 0 <= args.cooldown <= 60:
        parser.error('Use 5-180 seconds, unique/increasing counts between 1 and 4, and cooldown 0-60.')
    state = json.loads((ROOT / 'state.json').read_text())
    if state['status'] != 'ready':
        raise SystemExit('Run python deploy.py up first, then read the new state.json.')
    config = state['config']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = ROOT / 'evidence' / ('stress-' + stamp)
    folder.mkdir(parents=True)
    gc = Gcloud(config, folder)
    runtime = dict(serverIp=state['serverIp'], objectPrefix=config['objectPrefix'],
                   maxFileIndex=config['maxFileIndex'], port=config['port'],
                   serverVm=config['prefix'] + '-server', clientVm=config['prefix'] + '-client',
                   projectId=config['projectId'], owner=state['owner'])
    save(folder / 'workload-config.json', runtime)
    save(folder / 'deployment.json', state)
    shutil.copyfile(ROOT / 'stress_worker.py', folder / 'stress_worker.py')
    # Validate actual VM presence/ownership rather than trusting an old ready flag.
    snapshots = []
    for resource in state['resources']:
        if resource['kind'] == 'instance':
            remote = gc.describe(resource['describe'])
            if not remote or not owned(resource, remote, state['owner']) or remote['status'] != 'RUNNING':
                raise SystemExit('A tracked VM is absent, stopped, or has different ownership. Resolve deployment first.')
            snapshots.append(dict(name=resource['name'], remote=remote))
    save(folder / 'vm-config.json', snapshots)
    remotes = {role: '/tmp/hw4-stress-' + stamp for role in ('server', 'client')}
    started = []
    total = len(args.counts) * args.seconds + (len(args.counts) - 1) * args.cooldown + 5
    status = None
    try:
        for role, remote in remotes.items():
            ssh(gc, config, role, 'mkdir -p ' + shlex.quote(remote))
            upload(gc, config, role, folder / 'stress_worker.py', remote + '/stress_worker.py')
        upload(gc, config, 'client', folder / 'workload-config.json', remotes['client'] + '/config.json')
        for role, remote in remotes.items():
            base = ['python3', remote + '/stress_worker.py', 'sample' if role == 'server' else 'run',
                    '--directory', remote, '--seconds', str(total + 120 if role == 'server' else args.seconds)]
            if role == 'server':
                base += ['--unit', config['prefix'] + '-server']
            else:
                base += ['--config', remote + '/config.json', '--cooldown', str(args.cooldown),
                         '--counts', *map(str, args.counts)]
            command = 'nohup ' + shlex.join(base) + ' > ' + shlex.quote(remote + '/worker.log') + ' 2>&1 < /dev/null &'
            ssh(gc, config, role, command)
            started.append(role)
            if role == 'server':
                # First successful sample proves the unit/PID and /proc counters are readable.
                time.sleep(1)
                ssh(gc, config, role, 'test -s ' + shlex.quote(remote + '/server-metrics.jsonl') +
                    ' || { cat ' + shlex.quote(remote + '/worker.log') + '; exit 1; }')
        print(f'Running {args.counts}, {args.seconds}s each. Only these workload processes stop automatically.', flush=True)
        print('Evidence: ' + str(folder), flush=True)
        deadline = time.monotonic() + total + 120
        while time.monotonic() < deadline:
            status = poll_status(gc, config, remotes['client'])
            print('Progress: ' + json.dumps(status), flush=True)
            if status['status'] in ('complete', 'failed'):
                break
            time.sleep(10)
        else:
            raise RuntimeError('Workload exceeded its bounded runtime. Stop requested; review partial evidence.')
    finally:
        # Ctrl+C also requests remote cancellation; workload deadlines remain a second bound.
        for role in started:
            remote = remotes[role]
            try:
                ssh(gc, config, role, shlex.join(['python3', remote + '/stress_worker.py', 'stop',
                                                 '--directory', remote]), timeout=60)
            except Exception as error:
                print(f'Stop request for {role} failed: {error}. The worker remains time bounded.', flush=True)
        if started:
            time.sleep(4)
        for role in started:
            try:
                download(gc, config, role, remotes[role], folder)
            except Exception as error:
                print(f'Export for {role} failed: {error}; remote directory {remotes[role]} retained.', flush=True)
        print('Saved workload evidence: ' + str(folder), flush=True)
    if status is None or status['status'] != 'complete':
        if (folder / 'client/run.json').exists() and (folder / 'server/server-metrics.jsonl').exists():
            try:
                summary = summarize(folder)
                print('Completed load levels saved: ' + str([s['clients'] for s in summary['stages']]), flush=True)
            except Exception as error:
                print(f'Partial summary unavailable: {error}. Raw exports remain saved.', flush=True)
        detail = (status or {}).get('error') or 'No verified completion status was received.'
        raise SystemExit('Stress suite did not complete: ' + detail + '\nInspect ' +
                         str(folder / 'client/worker.log') + ' and the saved partial summary before retrying.')
    summary = summarize(folder)
    print(json.dumps(summary, indent=2))
    print('Create graphs: python plot_stress.py --folder "' + str(folder) + '"', flush=True)
    if not summary['measurement_valid']:
        raise SystemExit('Measurement verification failed; these samples are not complete report evidence.')


if __name__ == '__main__':
    main()
