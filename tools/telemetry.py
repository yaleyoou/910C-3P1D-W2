"""Scrape the four engines and segment ownership on P1 in one clock domain."""
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

roles = [s for s in ('P1', 'P2', 'P3', 'D') if os.environ.get(s)]
ip = {s: os.environ[s] for s in roles}
endpoints = {s: f'http://{ip[s]}:{8200 if s == "D" else 8100}/metrics' for s in roles}
endpoints['master'] = f'http://{ip["P1"]}:50089/metrics'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def values(text, name, label=None):
    result=[]
    for line in text.splitlines():
        if line.split('{',1)[0].split(' ',1)[0] != name: continue
        if label is None or label in line: result.append(float(line.rsplit(' ',1)[-1]))
    return result

def required(text,name,label=None):
    v=values(text,name,label)
    if not v: raise ValueError(f'missing {name} {label}')
    return sum(v)

def scrape(role):
    with opener.open(endpoints[role],timeout=6) as response:
        data=response.read().decode()
    if role=='master':
        gauges={}
        for name in ('segment_allocated_bytes','segment_total_capacity_bytes'):
            for seat,addr in ip.items():
                numbers=values(data,name,'segment="'+addr+':')
                expected=int(os.environ['D_STORE' if seat=='D' else 'P_STORE'])
                if not numbers and expected: raise ValueError(f'missing {name} at {seat}')
                gauges[seat+'_'+name]=sum(numbers)/2**30
        gauges['registered']=required(data,'master_total_capacity_bytes')/2**30
        gauges['allocated']=required(data,'master_allocated_bytes')/2**30
        return gauges
    if role=='D':
        kv=values(data,'vllm:kv_cache_usage_perc') or values(data,'vllm:gpu_cache_usage_perc')
        if not kv: raise ValueError('missing vllm:kv_cache_usage_perc on D')
        dp_run=values(data,'vllm:num_requests_running')
        dp_wait=values(data,'vllm:num_requests_waiting')
        dp_cap=values(data,'vllm:num_requests_waiting_by_reason','reason="capacity"')
        return {'capacity':required(data,'vllm:num_requests_waiting_by_reason','reason="capacity"'),
                'kv_usage':sum(kv)/len(kv),
                'running':sum(dp_run),
                'waiting':sum(dp_wait),
                'dp_running':dp_run,
                'dp_waiting':dp_wait,
                'dp_capacity':dp_cap,
                'dp_kv_usage':kv}
    res={key:required(data,'vllm:'+name) for key,name in {
        'waiting':'num_requests_waiting','query':'prefix_cache_queries_total',
        'local_hit':'prefix_cache_hits_total','ext_hit':'external_prefix_cache_hits_total'}.items()}
    p_run=values(data,'vllm:num_requests_running')
    p_wait=values(data,'vllm:num_requests_waiting')
    p_kv=values(data,'vllm:kv_cache_usage_perc') or values(data,'vllm:gpu_cache_usage_perc')
    res['running']=sum(p_run)
    res['kv_usage']=(sum(p_kv)/len(p_kv)) if p_kv else 0.0
    res['dp_running']=p_run
    res['dp_waiting']=p_wait
    res['dp_kv_usage']=p_kv
    return res

def sample():
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        data=dict(zip(endpoints,pool.map(scrape,endpoints)))
    data['time']=time.time()
    return data

if __name__=='__main__':
    target=Path(sys.argv[1])
    with target.open('x',buffering=1) as out:
        while True:
            t0=time.time()
            try:
                print(json.dumps(sample()),file=out)
            except Exception as exc:
                print(f'telemetry sample warning: {exc}',file=sys.stderr,flush=True)
            time.sleep(max(0.0,2.0-(time.time()-t0)))

