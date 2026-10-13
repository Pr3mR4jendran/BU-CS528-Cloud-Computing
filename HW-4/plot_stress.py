"""Summarize actual stress exports and generate report PNG/PDF graphs locally."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def window(rows, stage):
    return [row for row in rows if row.get('interval_start') is not None
            and row['interval_start'] >= stage['startEpoch'] and row['epoch'] <= stage['endEpoch']]


def metrics(rows):
    result = dict(samples=len(rows), covered_seconds=sum(row['epoch'] - row['interval_start'] for row in rows))
    for field in ('cpu_pct', 'steal_pct', 'iowait_pct', 'memory_used_mib', 'server_rss_mib',
                  'server_cpu_pct', 'rx_mbps', 'tx_mbps'):
        pairs = [(row[field], row['epoch'] - row['interval_start']) for row in rows if row.get(field) is not None]
        result[field + '_mean'] = sum(v * weight for v, weight in pairs) / sum(weight for _, weight in pairs) if pairs else None
        result[field + '_peak'] = max((v for v, _ in pairs), default=None)
    return result


def summarize(folder):
    run = json.loads((folder / 'client/run.json').read_text())
    server = jsonl(folder / 'server/server-metrics.jsonl')
    client = jsonl(folder / 'client/client-metrics.jsonl')
    stages = []
    for stage in run['stages']:
        if 'endEpoch' not in stage:
            continue
        count, duration = stage['clients'], stage['endEpoch'] - stage['startEpoch']
        records = []
        for path in (folder / 'client').glob(f'clients-{count}-slot-*.jsonl'):
            records.extend(jsonl(path))
        responses = [r for r in records if r['kind'] == 'response']
        counts = Counter(str(r['status']) for r in responses)
        unexpected = sum(r['status'] not in (200, 400) for r in responses)
        invalid = sum(r['status'] in (200, 400) and
                      (not r['body_valid'] or not r['event_id'] or r['response_bytes'] is None) for r in responses)
        transport = sum(r['kind'] == 'transport_failure' for r in records)
        timeouts = sum(r['kind'] == 'transport_failure' and r.get('reason') == 'client_timeout' for r in records)
        crashes = sum(r['kind'] == 'process_exit' and not r['planned'] for r in records)
        srows, crows = window(server, stage), window(client, stage)
        sm, cm = metrics(srows), metrics(crows)
        clients_peak = max((r['active_clients'] for r in crows), default=0)
        measured = sm['covered_seconds'] >= max(1, duration - 4) and cm['covered_seconds'] >= max(1, duration - 4)
        measured = measured and clients_peak == count and len(responses) > 0
        stages.append(dict(clients=count, startedAt=stage['startedAt'], duration_seconds=duration,
                           status_counts=dict(counts), completed_responses=len(responses),
                           responses_per_second=len(responses) / duration,
                           successful_downloads=counts.get('200', 0), expected_denials=counts.get('400', 0),
                           download_mib_per_second=sum(r['response_bytes'] or 0 for r in responses if r['status'] == 200) / duration / 1048576,
                           unexpected_http=unexpected, invalid_responses=invalid, transport_failures=transport,
                           request_timeouts=timeouts,
                           client_process_failures=crashes,
                           boundary_interrupted=sum(r['kind'] == 'boundary_interrupted' for r in records),
                           failure_observed=bool(unexpected or invalid or transport or crashes),
                           client_cpu_warning=(cm['cpu_pct_peak'] or 0) >= 85,
                           active_clients_peak=clients_peak, measurement_valid=measured,
                           server=sm, client=cm))
    valid = run['status'] == 'complete' and len(stages) == len(run['counts']) and all(s['measurement_valid'] for s in stages)
    summary = dict(measurement_valid=valid, run_status=run['status'], run_error=run.get('error'),
                   incomplete_load_levels=[s['clients'] for s in run['stages'] if 'endEpoch' not in s],
                   report_ready=valid and run['counts'] == [1, 2, 3, 4] and run['seconds'] == 180,
                   counts=run['counts'], seconds_per_level=run['seconds'],
                   first_failure_clients=next((s['clients'] for s in stages if s['failure_observed']), None),
                   stages=stages,
                   interpretation='Unexpected failures are signals requiring attribution, not automatic proof of server saturation. Expected 400 responses and cutoff interruptions are excluded.',
                   metric_definitions='CPU excludes idle/iowait/steal; memory is MemTotal-MemAvailable; NIC rates include GCS/API traffic; peaks are one-second sampled interval peaks, not instantaneous peaks. Server RSS is the Python process; CPU process percentage uses one core as 100%.')
    (folder / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    rows = []
    for stage in stages:
        row = {k: v for k, v in stage.items() if k not in ('server', 'client', 'status_counts')}
        for role in ('server', 'client'):
            row.update({role + '_' + k: v for k, v in stage[role].items()})
        row.update({'status_' + k: v for k, v in stage['status_counts'].items()})
        rows.append(row)
    if rows:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with (folder / 'summary.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fields)
            writer.writeheader()
            writer.writerows(rows)
    lines = ['# Measured stress results', '', f"Report-ready full experiment: {summary['report_ready']}", '',
             f"Worker status: {run['status']}. Only completed load levels are tabulated below.", '',
             '| Clients | Seconds | Responses/s | 200 | Expected 400 | Unexpected HTTP | Transport failures | Of which timeouts | Client exits |',
             '|---|---|---|---|---|---|---|---|---|']
    if run.get('error'):
        lines[6:6] = [f"Worker error: {run['error']}", '']
    for s in stages:
        lines.append(f"| {s['clients']} | {s['duration_seconds']:.0f} | {s['responses_per_second']:.2f} | {s['successful_downloads']} | {s['expected_denials']} | {s['unexpected_http']} | {s['transport_failures']} | {s['request_timeouts']} | {s['client_process_failures']} |")
    lines += ['', '| Clients | CPU mean/peak (%) | Host memory mean/peak (MiB) | Python RSS peak (MiB) | NIC RX mean/peak (Mbit/s) | NIC TX mean/peak (Mbit/s) |',
              '|---|---|---|---|---|---|']
    for s in stages:
        m = s['server']
        pair = lambda field: f"{m[field + '_mean']:.2f} / {m[field + '_peak']:.2f}" if m[field + '_mean'] is not None else 'unavailable'
        rss = f"{m['server_rss_mib_peak']:.2f}" if m['server_rss_mib_peak'] is not None else 'unavailable'
        lines.append(f"| {s['clients']} | {pair('cpu_pct')} | {pair('memory_used_mib')} | {rss} | {pair('rx_mbps')} | {pair('tx_mbps')} |")
    lines += ['', summary['interpretation'], '', summary['metric_definitions'], '',
              'Verbose client output is drained locally on the client VM; full HTML bodies are discarded. This instrumentation and the shared client VM can limit the offered workload. Response timestamps are output observations; no per-request latency claim is made.', '',
              'The seed for each client slot is fixed across load levels. Each process issues sequential requests; concurrency denotes provided-client processes, not a guaranteed number of simultaneous in-flight HTTP requests. Automatic retries after an early client exit are bounded to three attempts per slot.', '',
              'The experiment uses the public server endpoint. NIC rates include all traffic on the default interface. CPU/memory mean values are weighted by sampling interval duration. Keep raw samples and compact client stdout/stderr alongside the graphs.', '',
              'Billing/cost accounting is not generated by these graphs. Use deployment, experiment and cleanup timestamps with the project Billing report.']
    if summary['first_failure_clients'] is None and stages:
        lines += ['', f"No unexpected failures were recorded in the completed workloads up to {max(s['clients'] for s in stages)} clients. Incomplete levels are excluded from this statement. This does not establish the server's maximum capacity."]
    if summary['incomplete_load_levels']:
        lines += ['', f"Incomplete load levels: {summary['incomplete_load_levels']}. Keep their raw output for diagnosis; they are excluded from the completed-level tables."]
    for s in stages:
        if s['client_cpu_warning']:
            lines += ['', f"Client VM reached at least 85% CPU with {s['clients']} clients; examine whether load generation limited throughput."]
    (folder / 'results.md').write_text('\n'.join(lines) + '\n')
    return summary


def plot(folder, summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    out = folder / 'graphs'
    out.mkdir(exist_ok=True)
    stages, counts = summary['stages'], summary['counts']
    label = '180 seconds per level' if summary['report_ready'] else 'SMOKE / INCOMPLETE: not the full report experiment'

    def export(fig, name):
        fig.savefig(out / (name + '.png'), dpi=220, bbox_inches='tight')
        fig.savefig(out / (name + '.pdf'), bbox_inches='tight')
        plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5), constrained_layout=True)
    panels = [(('cpu_pct_mean', 'Mean CPU'), ('cpu_pct_peak', 'Peak CPU')),
              (('memory_used_mib_mean', 'Mean host memory'), ('memory_used_mib_peak', 'Peak host memory'), ('server_rss_mib_peak', 'Peak Python RSS')),
              (('rx_mbps_mean', 'Mean receive'), ('rx_mbps_peak', 'Peak receive'), ('tx_mbps_mean', 'Mean transmit'), ('tx_mbps_peak', 'Peak transmit'))]
    for ax, entries, ylabel, title in zip(axes, panels, ['CPU (%)', 'Memory (MiB)', 'Bandwidth (Mbit/s)'], ['Server CPU', 'Server memory', 'Server NIC traffic']):
        for key, legend in entries:
            ax.plot(counts, [s['server'][key] for s in stages], marker='o', label=legend)
        ax.set(xlabel='Concurrent client processes', ylabel=ylabel, title=title, xticks=counts)
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    axes[0].set_ylim(bottom=0, top=100)
    axes[1].set_ylim(bottom=0)
    axes[2].set_ylim(bottom=0)
    fig.suptitle('Measured server resource usage: ' + label)
    export(fig, 'performance')

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5), constrained_layout=True)
    axes[0].plot(counts, [s['responses_per_second'] for s in stages], 'o-', label='All completed responses')
    axes[0].plot(counts, [s['successful_downloads'] / s['duration_seconds'] for s in stages], 'o-', label='HTML downloads')
    axes[0].set(ylabel='Responses / second', title='Throughput')
    axes[0].legend(fontsize=8)
    for key, legend in [('unexpected_http', 'Unexpected HTTP'), ('invalid_responses', 'Invalid response'), ('transport_failures', 'Transport failure'), ('client_process_failures', 'Early client exit')]:
        axes[1].plot(counts, [s[key] for s in stages], 'o-', label=legend)
    axes[1].set(ylabel='Observed count', title='Failure signals (expected 400 excluded)')
    maximum_failures = max((s[key] for s in stages for key in
        ('unexpected_http', 'invalid_responses', 'transport_failures', 'client_process_failures')), default=0)
    axes[1].set_ylim(0, max(1, maximum_failures * 1.15))
    axes[1].yaxis.set_major_locator(MaxNLocator(integer=True))
    axes[1].legend(fontsize=8)
    for key, legend in [('cpu_pct_mean', 'Mean client VM CPU'), ('cpu_pct_peak', 'Peak client VM CPU')]:
        axes[2].plot(counts, [s['client'][key] for s in stages], 'o-', label=legend)
    axes[2].set(ylabel='CPU (%)', title='Load generator diagnostic', ylim=(0, 100))
    axes[2].legend(fontsize=8)
    for ax in axes:
        ax.set(xlabel='Concurrent client processes', xticks=counts)
        ax.grid(alpha=.25)
    axes[0].set_ylim(0, max(1, max(s['responses_per_second'] for s in stages) * 1.15))
    fig.suptitle('Client outcomes: ' + label)
    export(fig, 'throughput-and-errors')

    run = json.loads((folder / 'client/run.json').read_text())
    server = jsonl(folder / 'server/server-metrics.jsonl')
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True, constrained_layout=True)
    for stage in run['stages']:
        rows = window(server, stage)
        x = [(r['epoch'] - stage['startEpoch']) / 60 for r in rows]
        for ax, field in zip(axes, ['cpu_pct', 'memory_used_mib', 'tx_mbps']):
            ax.plot(x, [r[field] for r in rows], label=f"{stage['clients']} clients", linewidth=1)
    for ax, title, ylabel in zip(axes, ['Server CPU', 'Server host memory', 'Server NIC transmit bandwidth'], ['CPU (%)', 'Memory (MiB)', 'Mbit/s']):
        ax.set(title=title, ylabel=ylabel)
        ax.grid(alpha=.25)
        ax.legend(ncol=4, fontsize=8)
    axes[0].set_ylim(0, 100)
    axes[-1].set_xlabel('Minutes since start of each workload')
    fig.suptitle('Measured resource time series: ' + label)
    export(fig, 'resource-timeseries')
    print('Graphs saved to ' + str(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--folder', type=Path)
    choice.add_argument('--latest', action='store_true')
    args = parser.parse_args()
    if args.latest:
        candidates = [p for p in (Path(__file__).parent / 'evidence').glob('stress-*') if (p / 'client/run.json').exists()]
        if not candidates:
            raise SystemExit('No exported stress experiment exists.')
        folder = max(candidates, key=lambda p: p.name)
    else:
        folder = args.folder.resolve()
    summary = summarize(folder)
    if not summary['measurement_valid']:
        raise SystemExit('Incomplete measurements; summary saved, but report graphs refused.')
    plot(folder, summary)
    print((folder / 'results.md').read_text())


if __name__ == '__main__':
    main()
