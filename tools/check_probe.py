"""Check one cold, known-prefix handoff through each P before the measured passes."""
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
probes=json.loads((root/'probe-keys.json').read_text())
ev=[json.loads(x) for x in (root/'logs/d-blocks.jsonl').read_text().splitlines()]
alloc=[e for e in ev if e['event']=='alloc']
assert len(alloc)==len(probes)>=1
ns=None
for row,keys in zip(alloc,probes):
    assert {x['event'] for x in ev if x['req']==row['req']}=={'alloc','ready','finish'}
    hashes=set(row['hashes'])
    assert len(hashes)==80 and row['block_size']==128
    by={}
    for k in keys:
        prefix,_,h=k.rpartition('@')
        if h in hashes: by.setdefault(prefix,set()).add(h)
    match,=[k for k,v in by.items() if v==hashes]
    assert ns is None or ns==match
    ns=match
(root/'known-match.json').write_text(json.dumps(dict(hashes=80,matching_extents={ns:80})))
print(f'{len(alloc)} prefiller path(s) and Store namespace verified')

