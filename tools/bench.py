"""Run the frozen workload on P1. Persist raw counters bracketing all requests."""
import os, json, subprocess, sys, time
from pathlib import Path
from telemetry import sample
p=Path('/run-data')
ip=os.environ['P1']
case=os.environ['CASE']
phase=os.environ['PHASE']
assert phase in ('perf','blocks') and os.environ['ROLE']=='p1'
before=sample()
for role in ('P1','P2','P3','D'):
    expected=int(os.environ['D_STORE' if role=='D' else 'P_STORE'])
    actual=before['master'][role+'_segment_total_capacity_bytes']
    assert abs(expected-actual)<1e-6,(role,expected,actual)
assert before['master']['allocated']==0,'Restart all four containers for cold Store'
assert not (p/'before.json').exists(),'Run directory already used'
(p/'before.json').write_text(json.dumps(before))
children=[]
try:
    if phase=='blocks':
        children.append(subprocess.Popen([sys.executable,'/deploy/tools/store_snapshot.py',f'http://{ip}:50089/get_all_keys',str(p/'store.jsonl.gz'),'--seconds','5']))
    children.append(subprocess.Popen([sys.executable,'/deploy/tools/telemetry.py',str(p/'metrics.jsonl')]))
    time.sleep(5)
    assert all(proc.poll() is None for proc in children)
    subprocess.run([sys.executable,'/deploy/tools/replay_frozen.py','/deploy/data/checkpoints/W2/frozen',str(p/'finite-client'),'16',f'http://{ip}:8300/v1/chat/completions'],check=True)
    time.sleep(8)
    (p/'after.json').write_text(json.dumps(sample()))
    assert all(proc.poll() is None for proc in children),'sampler failed'
finally:
    for proc in children: proc.terminate()
    for proc in children: proc.wait()

