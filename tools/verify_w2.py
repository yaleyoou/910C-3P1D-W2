import gzip, hashlib, json, sys
from pathlib import Path
root=Path(sys.argv[1])
content=(root/'requests.jsonl').read_bytes()
manifest=json.loads((root/'manifest.json').read_text())
rows=[json.loads(line) for line in content.splitlines()]
assert hashlib.sha256(content).hexdigest()==manifest['requests_sha256']=='ef49990e211e47b373bfccf1981d60c8a9eaf9a9b4049bb55276c30e4441c106'
ids={r['request_id'] for r in rows}
assert len(rows)==len(ids)==985 and len(set(manifest['root_order']))==20
assert sum(len(r['dependencies']) for r in rows)==1006
assert all(set(r['dependencies'])<=ids for r in rows)
assert sum(r['reference_prompt_tokens'] for r in rows)==81245507 and sum(r['target_output_tokens'] for r in rows)==984784
for digest in {r['body_sha256'] for r in rows}:
    path=root/manifest['body_dir']/(digest+'.json.gz')
    assert hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()==digest,path
print('W2 verified: 985 requests, 20 roots, 1006 edges, 81245507 input, 984784 output.')

