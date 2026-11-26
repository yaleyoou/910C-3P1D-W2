import hashlib,json
from pathlib import Path
base=Path(__file__).resolve().parents[1]
hashes=json.loads((base/'evidence/runtime-libraries.json').read_text())
lib=base/'upgrades/memfabric/v131-4t/memfabric_hybrid/lib'
for name,expected in hashes.items():
    file=lib/name
    if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=expected:
        raise SystemExit(f'Incomplete 4T runtime: {file}')
print(f'{len(hashes)} historical MemFabric shared libraries verified')

