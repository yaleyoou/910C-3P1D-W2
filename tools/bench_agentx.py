#!/usr/bin/env python3
"""Run zero-delay AgentX replay inside P1 container with P/D/Master telemetry."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, '/deploy/tools')
from telemetry import sample, scrape

p = Path('/run-data')
ip = os.environ['P1']
trace_rel = os.environ.get('AGENTX_TRACES', '/deploy/data/traces.jsonl')
duration = os.environ.get('AGENTX_DURATION', '3600')
concurrency = os.environ.get('AGENTX_CONCURRENCY', '16')
hard_stop = os.environ.get('AGENTX_HARD_STOP', '0') == '1'
allow_warm = os.environ.get('AGENTX_ALLOW_WARM', '0') == '1'

assert os.environ['ROLE'] == 'p1'
assert Path(trace_rel).is_file(), f'Trace file not found inside container: {trace_rel}'

active_p = [r for r in ('P1', 'P2', 'P3') if os.environ.get(r)]
active_roles = active_p + ['D']

# Wait up to 300s for active nodes' Mooncake Store segments (including delayed store_d) to register
deadline = time.time() + 300
last_err = None
while time.time() < deadline:
    try:
        m = scrape('master')
        ok = True
        for role in active_roles:
            exp = int(os.environ['D_STORE' if role == 'D' else 'P_STORE'])
            got = m[role + '_segment_total_capacity_bytes']
            if abs(got - exp) >= 1e-6:
                ok = False
                last_err = f'{role}: registered {got:.3f} GiB != expected {exp} GiB'
                break
        if ok:
            break
    except Exception as exc:
        last_err = str(exc)
    time.sleep(3)
else:
    raise SystemExit(f'Timed out waiting for Mooncake Store segments: {last_err}')

(p / 'params.json').write_text(json.dumps({
    'case': os.environ['CASE'],
    'ps': int(os.environ['P_STORE']),
    'p_count': len(active_p),
    'ds': int(os.environ['D_STORE']),
    'sparse': int(os.environ['SPARSE']),
    'hosts': [os.environ[k] for k in active_roles],
    'duration_s': float(duration),
    'concurrency': int(concurrency),
    'traces': trace_rel,
}))

before = sample()
if not allow_warm:
    assert before['master']['allocated'] == 0, 'Restart all four containers for cold Store (or set AGENTX_ALLOW_WARM=1)'
assert not (p / 'before.json').exists(), 'Run directory already used'
(p / 'before.json').write_text(json.dumps(before))

tel = subprocess.Popen([sys.executable, '/deploy/tools/telemetry.py', str(p / 'metrics.jsonl')])
try:
    time.sleep(5)
    assert tel.poll() is None, 'telemetry failed to start'
    cmd = [
        sys.executable, '/deploy/tools/replay_agentx.py',
        trace_rel, str(p / 'agentx-client'),
        '--url', f'http://{ip}:8300/v1/completions',
        '--duration', str(duration),
        '--concurrency', str(concurrency),
    ]
    if hard_stop:
        cmd.append('--hard-stop')
    subprocess.run(cmd, check=True)
    time.sleep(8)
    (p / 'after.json').write_text(json.dumps(sample()))
    assert tel.poll() is None, 'telemetry sampler died during replay'
finally:
    tel.terminate()
    tel.wait()

for scope, suffix in (('total', ''), ('window', '-window')):
    csv_out = subprocess.check_output([
        sys.executable, '/deploy/tools/summarize_agentx.py', str(p),
        '--scope', scope,
        *(('--details', str(p / 'per-P.json')) if scope == 'total' else ()),
    ], text=True)
    md_out = subprocess.check_output([
        sys.executable, '/deploy/tools/summarize_agentx.py', str(p),
        '--scope', scope, '--markdown',
    ], text=True)
    (p / f'result{suffix}.csv').write_text(csv_out, encoding='utf-8')
    (p / f'result{suffix}.md').write_text(md_out, encoding='utf-8')

print((p / 'result.md').read_text(encoding='utf-8'))
