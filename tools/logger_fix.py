from pathlib import Path
import hashlib
p=Path('/vllm-workspace/vllm-ascend/vllm_ascend/logger.py')
old='f7907aaf0055f0cb6c0f9c917c93447da94f40a122acc3cfe03e2f6aa5633581'
new='b8a4eb1f4d34734e597c5e638291f80ea0df0996a3602c0941d56c81054ebbcb'
b=p.read_bytes()
if hashlib.sha256(b).hexdigest()!=new:
    assert hashlib.sha256(b).hexdigest()==old, 'unexpected logger source; verify image'
    b=b.replace(b'os.path.join(os.path.expanduser("~"), "ascend", "log", "vllm_ascend")', b'"/dev/shm/glm52-ascend-logs"')
    assert hashlib.sha256(b).hexdigest()==new
    p.write_bytes(b)

