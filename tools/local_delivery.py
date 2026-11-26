"""Verify delivery on the Mac after VS Code finishes writing its image."""
import hashlib
import subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[1]
def digest(reader):
    h=hashlib.sha256()
    for chunk in iter(lambda: reader.read(8*1024*1024),b''):
        h.update(chunk)
    return h.hexdigest()
for line in (root/'assets.sha256').read_text().splitlines():
    expected,name=line.split()
    with (root/name).open('rb') as f:
        if digest(f)!=expected: raise SystemExit('Incomplete or corrupt: '+name)
with subprocess.Popen(['zstd','-dc',str(root/'image.tar.zst')],stdout=subprocess.PIPE) as dec:
    raw=digest(dec.stdout)
    if dec.wait() or raw!='e07158bf61731488e237d2f7a3fe1730779d11c141d045e3c4df8367835e9965':
        raise SystemExit('Invalid original Docker tar')
subprocess.run(['python3',str(root/'tools/verify_runtime.py')],check=True)
subprocess.run(['python3',str(root/'tools/verify_w2.py'),str(root/'data/checkpoints/W2/frozen')],check=True)
p=root/'README.md'
lines=p.read_text().splitlines()
for i,line in enumerate(lines):
    if line.startswith('> 本地交付进度'):
        lines[i]='> 本地交付已校验。原 Docker 镜像压缩包与解压 tar 均通过 SHA-256 校验，985 个 W2 请求体与 7 个实机补丁库均匹配历史记录。'
p.write_text('\n'.join(lines)+'\n')
(root/'已完成校验.txt').write_text('Image archive SHA-256 '+raw+'\nW2 and 4T shared libraries verified.\n')
