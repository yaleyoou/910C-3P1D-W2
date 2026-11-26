#!/usr/bin/env bash
# On old node25, as a user with sudo: export only this experiment's assets into a NEW folder.
set -euo pipefail
dest=${1:?new empty export directory}
root=/home/youxingye/youxingye_data/glm52-w4a8c8-pools
ref="$root/results/reference5h-s256-c16-st1024-20261118"
mkdir "$dest";cd "$dest"
sudo tar czf - --exclude='__pycache__' --exclude='*.pyc' --exclude='*/logs/*' -C "$root" upgrades/memfabric/v131-4t upgrades/memfabric/enable_4t.sh upgrades/memfabric/patch-artifacts/manifest.json > memfabric-complete.tar.gz
python3 - "$ref" <<'PY'
import sys,json,hashlib,tarfile
from pathlib import Path
r=Path(sys.argv[1]);f=r/'checkpoints/W2/frozen';m=json.loads((f/'manifest.json').read_text())
raw=(f/'requests.jsonl').read_bytes()
assert hashlib.sha256(raw).hexdigest()=='ef49990e211e47b373bfccf1981d60c8a9eaf9a9b4049bb55276c30e4441c106'
rows=[json.loads(x) for x in raw.splitlines()]
with tarfile.open('w2-data.tar.gz','w:gz',compresslevel=2) as tf:
    for n in ('manifest.json','requests.jsonl'): tf.add(f/n,arcname='checkpoints/W2/frozen/'+n)
    for h in sorted({x['body_sha256'] for x in rows}):
        n=h+'.json.gz';tf.add(f/m['body_dir']/n,arcname='capture/bodies/'+n)
PY
image=/tgm_rent_data/data/youxingye/glm52-image-transfer/image.tar
test "$(sha256sum "$image" | cut -d' ' -f1)" = e07158bf61731488e237d2f7a3fe1730779d11c141d045e3c4df8367835e9965
zstd -q -T4 -4 -c "$image" > image.tar.zst
sha256sum image.tar.zst w2-data.tar.gz > assets.sha256

