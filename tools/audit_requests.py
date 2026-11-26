import collections
import hashlib
import json
import sys
from pathlib import Path

root=Path(sys.argv[1])
frozen=Path('/deploy/data/checkpoints/W2/frozen')
def rows(path):
    return [json.loads(line) for line in path.open() if line.endswith(chr(10))]
manifest=json.loads((frozen/'manifest.json').read_text())
assert hashlib.sha256((frozen/'requests.jsonl').read_bytes()).hexdigest()==manifest['requests_sha256']
expected={r['request_id']:r for r in rows(frozen/'requests.jsonl')}
events=rows(root/'finite-client/events.jsonl'); sends=[r for r in events if r['event']=='send']; done=[r for r in events if r['event']=='done']; start=min(r['wall_time'] for r in sends)
assert len(expected)==985 and len({r['request_id'] for r in sends})==len(sends)
assert len({r['request_id'] for r in done})==len(done)
for r in done:
    ref=expected[r['request_id']]; assert r['body_sha256']==ref['body_sha256'] and r['usage']['completion_tokens']==ref['target_output_tokens']; assert ref.get('reference_prompt_tokens') in (None,r['usage']['prompt_tokens'])
d=collections.defaultdict(dict)
for r in rows(root/'logs/d-blocks.jsonl'):
    assert r['event'] not in d[r['req']]
    d[r['req']][r['event']]=r
clock=json.loads((root/'clock-before.json').read_text()); b=min(clock,key=lambda s:s['p_receive']-s['p_send']); offset=(b['d_clock']-(b['p_send']+b['p_receive'])/2)/1e9
sent_by_id={r['request_id']:r for r in sends}
http_to_d={r['http']:r['d_ids'] for r in rows(root/'logs/request-map.jsonl')}
assert len(http_to_d)==len(sends)==985
options={}
for c in done:
    choices=[]
    for ident,v in d.items():
        if 'finish' not in v or 'ready' not in v: continue
        if (v['alloc']['tokens'],v['finish']['tokens'])!=(c['usage']['prompt_tokens'],c['usage']['total_tokens']): continue
        delay=c['end_unix']-(v['finish']['t_ns']/1e9-offset)
        if abs(delay)<5: choices.append((abs(delay),ident))
    names=http_to_d[sent_by_id[c['request_id']]['http_request_id']]
    matches=[(delta,ident) for delta,ident in choices if any(ident==name or ident.startswith(name+'-') for name in names)]
    options[c['request_id']]=sorted(matches or choices)
selected={k:v[0][1] for k,v in options.items() if len(v)==1}
complete=(len(selected)==len(set(selected.values()))==len(done)==len(sends)==len(d)==985 and not any(r['event']=='error' for r in events))
print(json.dumps(dict(complete=complete,sent=len(sends),done=len(done),d_requests=len(d),unique_matches=len(selected),unmatched=sum(not v for v in options.values()),ambiguous=sum(len(v)>1 for v in options.values()),error_events=sum(r['event']=='error' for r in events)),indent=2))
