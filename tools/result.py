"""Combine a performance pass and a separately instrumented block-overlap pass."""
import argparse,csv,json,sys
from pathlib import Path
def read(p): return json.loads(p.read_text())
def rows(p): return [json.loads(x) for x in p.read_text().splitlines()]
def verify(folder):
    s=read(folder/'finite-client/summary.json')
    assert (s['requests'],s['failed'],s['input_tokens'],s['output_tokens'],s['roots'],s['concurrency'])==(985,0,81245507,984784,20,16)
    assert s['requests_sha256']=='ef49990e211e47b373bfccf1981d60c8a9eaf9a9b4049bb55276c30e4441c106'
    return s
def time_mean(samples,lo,hi,fn):
    area=covered=0.
    for x,y in zip(samples,samples[1:]):
        dt=min(hi,y['time'])-max(lo,x['time'])
        if dt>0:
            if y['time']-x['time']>20: raise ValueError('metrics gap >20s')
            area+=dt*fn(x); covered+=dt
    assert abs(hi-lo-covered)<0.01,('coverage',covered,hi-lo)
    return area/covered
def summarize(run, details=None):
    perf=run/'perf/p1'; block=run/'blocks/p1'
    s=verify(perf); verify(block)
    if not read(block/'request-audit.json')['complete']:raise ValueError('D trace audit incomplete')
    ov=read(block/'overlap.json')
    assert ov['complete'] and ov['snapshot_errors']==0
    o=ov['all_snapshots'];assert abs(o['covered_s']-ov['seconds'])<.1
    lo,hi=s['first_send_unix'],s['last_done_unix']
    a=read(perf/'before.json');z=read(perf/'after.json')
    samples=[a]+rows(perf/'metrics.jsonl')+[z]
    ps=['P1','P2','P3']
    def diff(p,k):
        v=[r[p][k] for r in samples]
        assert all(y>=x for x,y in zip(v,v[1:])),(p,k,'counter reset')
        return v[-1]-v[0]
    q=sum(diff(p,'query') for p in ps)
    assert all(diff(p,'query')>0 for p in ps),'traffic missing from a prefiller'
    h=sum(diff(p,'local_hit')+diff(p,'ext_hit') for p in ps)
    assert 0<=h<=q
    avg=lambda f:time_mean(samples,lo,hi,f)
    per_p={p:{'query':diff(p,'query'),'hit':diff(p,'local_hit')+diff(p,'ext_hit'),
              'queue':avg(lambda x:x[p]['waiting']),
              'store_GiB':avg(lambda x:x['master'][p+'_segment_allocated_bytes'])} for p in ps}
    g=read(perf/'params.json')
    assert g==read(block/'params.json')
    result={'组别':g['case'],'D Store / sparse（GiB）':f"{g['ds']} / {g['sparse']}",'总预算 GiB':g['ps']*3+g['ds']+g['sparse'],
      '输出 tok/s':s['output_tokens_per_s'],'TTFT 均值 s':s['ttft_mean_s'],'TTFT P95 s':s['ttft_p95_s'],
      '平均 TPOT ms':s['tpot_mean_ms'],'P 平均排队数（三台合计）':sum(v['queue'] for v in per_p.values()),
      'D 平均容量等待数':avg(lambda x:x['D']['capacity']),'P 合计前缀命中率 %':100*h/q,
      'P Store 平均占用 GiB':sum(v['store_GiB'] for v in per_p.values()),
      'D Store 平均占用 GiB':avg(lambda x:x['master']['D_segment_allocated_bytes']),
      'D 平均活跃主 KV GiB':o['mean_active_GiB'],'平均重复量 GiB':o['mean_overlap_GiB'],'活跃主 KV 重复率 %':o['ratio_pct']}
    if details is not None:
        details.write_text(json.dumps(per_p,indent=2))
    return result
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('runs',type=Path,nargs='+')
    parser.add_argument('--details',type=Path)
    args=parser.parse_args()
    if args.details and len(args.runs)!=1: parser.error('--details takes one run')
    results=[summarize(x,args.details) for x in args.runs]
    out=csv.DictWriter(sys.stdout,results[0].keys());out.writeheader()
    out.writerows([{k:round(v,3) if isinstance(v,float) else v for k,v in r.items()} for r in results])

