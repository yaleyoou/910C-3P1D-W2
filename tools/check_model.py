"""Check that the model bind mount contains readable, nonempty weight shards."""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
index = root / "model.safetensors.index.json"
if index.is_file():
    shards = sorted(set(json.loads(index.read_text())["weight_map"].values()))
else:
    shards = [p.name for p in root.glob("*.safetensors")]
if not shards:
    sys.exit(f"No safetensors weights found in {root}")
for name in shards:
    path = root / name
    if not path.is_file() or not path.stat().st_size:
        sys.exit(f"Missing or empty model shard: {path}")
    if root.resolve() not in path.resolve().parents:
        sys.exit(f"Shard points outside the model mount: {path}. Copy weights with rsync -aL.")
print(f"Found {len(shards)} weight files in {root}")
