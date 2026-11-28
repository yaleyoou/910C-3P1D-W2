"""Send one probe through each P and check D hashes against live Store keys."""
import json, os, sys, time
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
opener=build_opener(ProxyHandler({}))
root=Path('/run-data')


def keys():
    with opener.open('http://'+os.environ['P1']+':50089/get_all_keys',timeout=10) as resp:
        return resp.read().decode().splitlines()
snapshots=[]
p_count=sum(1 for k in ('P1','P2','P3') if os.environ.get(k))
for i in range(p_count):

    msg={'model':'glm-5','messages':[{'role':'user','content':f'F{i+1} overlap calibration. '+('apple pear orange banana. '*2048)}],'max_tokens':128,'ignore_eos':True,'temperature':0}
    req=Request(f'http://{os.environ["P1"]}:{8311+i}/v1/chat/completions',data=json.dumps(msg).encode(),headers={'Content-Type':'application/json'})
    with opener.open(req,timeout=600) as res: json.load(res)
    time.sleep(2)
    snapshots.append(keys())
(root/'probe-keys.json').write_text(json.dumps(snapshots))
