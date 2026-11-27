import collections
import gzip
import json
import pathlib
import statistics
import sys

root = pathlib.Path(sys.argv[1])
block = 128
bytes_per_token = 78 * (512 + 64) * 2
calibration = json.loads((root / 'known-match.json').read_text())
namespace, = calibration['matching_extents']
assert calibration['matching_extents'][namespace] == calibration['hashes'] == 80
clock = json.loads((root/'clock-before.json').read_text())
best = min(clock, key=lambda x: x['p_receive']-x['p_send'])
offset = best['d_clock'] - (best['p_send']+best['p_receive'])//2

def records(path):
    with path.open() as stream:
        for line in stream:
            if line.endswith(chr(10)):
                yield json.loads(line)

client = list(records(root/'finite-client/events.jsonl'))
sends = [r for r in client if r['event']=='send']
done = [r for r in client if r['event']=='done']
start = round(min(r['wall_time'] for r in sends)*1e9)
end = round(max(r['wall_time'] for r in done)*1e9)
by_id = collections.defaultdict(dict)
for row in records(root/'logs/d-blocks.jsonl'):
    if row['t_ns']-offset >= start:
        by_id[row['req']][row['event']] = row
complete = all('finish' in v and 'ready' in v for v in by_id.values()) and len(done)==len(sends)==len(by_id)==985
# Use only settled intervals for a live preview.
if not complete:
    pending = [v['alloc']['t_ns']-offset for v in by_id.values() if 'finish' not in v]
    if pending: end = min(end,min(pending))
requests = []
for rid,v in by_id.items():
    if 'finish' not in v or 'ready' not in v: continue
    a,f=v['alloc'],v['finish']; assert a['block_size']==block
    assert f['hashes'][:len(a['hashes'])] == a['hashes']
    requests.append((v['ready']['t_ns']-offset,f['t_ns']-offset,a['tokens'],f['tokens']-1,f['slots'],f['hashes']))
# One logical slot per physical D block, including shared prefix slots only once.
def sample(t,keys):
    lower={}; upper={}; middle={}; lo_match={}; hi_match={}; mid_match={}; unique={}
    for ready,finish,initial,final,slots,hashes in requests:
        if not ready<=t<finish: continue
        n0=min(initial,final); n1=max(n0,final); n=round(n0+(n1-n0)*(t-ready)/(finish-ready))
        for j,slot in enumerate(slots):
            pos=j*block
            if pos>=n1: break
            lo=max(0,min(block,n0-pos)); hi=min(block,n1-pos); mid=max(0,min(block,n-pos))
            lower[slot]=max(lower.get(slot,0),lo); upper[slot]=max(upper.get(slot,0),hi); middle[slot]=max(middle.get(slot,0),mid)
            if j<len(hashes) and hashes[j] in keys:
                lo_match[slot]=max(lo_match.get(slot,0),lo); hi_match[slot]=max(hi_match.get(slot,0),hi); mid_match[slot]=max(mid_match.get(slot,0),mid); unique[hashes[j]]=max(unique.get(hashes[j],0),mid)
            elif j>=len(hashes):
                hi_match[slot]=max(hi_match.get(slot,0),hi)
    return [sum(x.values()) for x in (lower,upper,middle,lo_match,hi_match,mid_match,unique)]

workload_total_tokens=sum(final for _,_,_,final,_,_ in requests)
ever_matched=[set() for _ in requests]
seen_requests=set()
keys=set(); snapshots=[]; errors=0; durations=[]
try:
    with gzip.open(root/'store.jsonl.gz','rt') as stream:
        for line in stream:
            r=json.loads(line)
            if 'error' in r: errors+=1; continue
            for values,method in ((r['removed'],keys.discard),(r['added'],keys.add)):
                for k in values:
                    ns,_,digest=k.rpartition('@')
                    if ns==namespace: method(digest)
            t=(r['begin_ns']+r['end_ns'])//2
            for idx,(ready,finish,initial,final,_,hashes) in enumerate(requests):
                if ready<=t<finish:
                    seen_requests.add(idx)
                    available=min(final,round(initial+(final-initial)*(t-ready)/(finish-ready)))
                    for j,digest in enumerate(hashes[:available//block]):
                        if digest in keys: ever_matched[idx].add(j)
            if t < start-10e9: continue
            snapshots.append((t,sample(t,keys)))
            durations.append((r['end_ns']-r['begin_ns'])/1e9)
            if t>end+10e9: break
except EOFError:
    pass
ever_overlapped_tokens=sum(len(m)*block for m in ever_matched)
workload_overlap_pct=(100*ever_overlapped_tokens/workload_total_tokens) if workload_total_tokens else 0.0

def integrate(rows):
    total=[0.0]*7; coverage=0
    for (a,x),(b,y) in zip(rows,rows[1:]):
        left=max(a,start); right=min(b,end)
        if left>=right: continue
        if b-a>30e9: raise ValueError('Store snapshot gap >30s')
        dt=(right-left)/1e9; coverage+=dt
        for j in range(7):
            frac=((left-a)+(right-a))/(2*(b-a)); total[j]+=(x[j]+frac*(y[j]-x[j]))*dt
    l,u,m,lm,um,mm,unique=total
    if not m: return dict(covered_s=coverage)
    return dict(covered_s=coverage,ratio_pct=100*mm/m,conditional_ratio_bounds_pct=[100*lm/u,min(100,100*um/l)],mean_overlap_GiB=mm/coverage*bytes_per_token/2**30,mean_unique_overlap_GiB=unique/coverage*bytes_per_token/2**30,mean_overlap_bounds_GiB=[lm/coverage*bytes_per_token/2**30,um/coverage*bytes_per_token/2**30],mean_active_GiB=m/coverage*bytes_per_token/2**30)
print(json.dumps(dict(complete=complete,sent=len(sends),done=len(done),d_ids=len(by_id),seconds=(end-start)/1e9,snapshot_errors=errors,samples=len(snapshots),workload_total_tokens=workload_total_tokens,ever_overlapped_tokens=ever_overlapped_tokens,workload_overlap_pct=workload_overlap_pct,seen_requests=len(seen_requests),all_snapshots=integrate(snapshots),every_other_snapshot=integrate(snapshots[::2]),max_snapshot_duration_s=max(durations,default=0)),indent=2))
