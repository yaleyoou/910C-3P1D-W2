import asyncio,aiohttp,json,gzip,time,sys,hashlib,uuid,statistics
from pathlib import Path
frozen=Path(sys.argv[1]);out=Path(sys.argv[2]);concurrency=int(sys.argv[3]) if len(sys.argv)>3 else 16;url=sys.argv[4] if len(sys.argv)>4 else 'http://127.0.0.1:8300/v1/chat/completions'
manifest=json.loads((frozen/'manifest.json').read_text());raw=(frozen/'requests.jsonl').read_bytes();assert hashlib.sha256(raw).hexdigest()==manifest['requests_sha256'];rows=[json.loads(s) for s in raw.splitlines()];byid={r['request_id']:r for r in rows};assert len(byid)==len(rows);out.mkdir(parents=True,exist_ok=False);events=out.joinpath('events.jsonl').open('x');results=[];running=0;peak=0;t0=None
for r in rows:
 assert all(byid[d]['root_id']==r['root_id'] for d in r['dependencies'])
def log(event,**kw):events.write(json.dumps({'event':event,'wall_time':time.time(),**kw})+chr(10));events.flush()
def percentile(v,p):
 v=sorted(v);return v[min(len(v)-1,int((len(v)-1)*p))] if v else None
async def main():
 global running,peak,t0
 ready={r['request_id']:asyncio.Event() for r in rows};sem=asyncio.Semaphore(concurrency);groups={root:[r for r in rows if r['root_id']==root] for root in manifest['root_order']}
 timeout=aiohttp.ClientTimeout(total=21600,sock_read=21600);connector=aiohttp.TCPConnector(limit=0)
 async with aiohttp.ClientSession(timeout=timeout,connector=connector,trust_env=False) as session:
  async def request(r):
   global running,peak,t0
   for dep in r['dependencies']:await ready[dep].wait()
   data=gzip.decompress(((frozen/manifest['body_dir']).resolve()/(r['body_sha256']+'.json.gz')).read_bytes());assert hashlib.sha256(data).hexdigest()==r['body_sha256'];start=time.time();perf=time.perf_counter();first=None;last=None;usage=None;finish=None;rid=r['request_id'];http_id=str(uuid.uuid4());running+=1;peak=max(peak,running)
   if t0 is None:t0=start
   log('send',request_id=rid,http_request_id=http_id,root_id=r['root_id'],body_sha256=r['body_sha256'],target_output_tokens=r['target_output_tokens'])
   try:
    async with session.post(url,data=data,headers={'Content-Type':'application/json','X-Request-Id':http_id}) as response:
     if response.status!=200:raise RuntimeError('HTTP '+str(response.status)+' '+(await response.text())[:1000])
     async for line in response.content:
      if not line.startswith(b'data:'):continue
      value=line[5:].strip()
      if value==b'[DONE]' or not value:continue
      chunk=json.loads(value)
      if chunk.get('usage'):usage=chunk['usage']
      for choice in chunk.get('choices',[]):
       delta=choice.get('delta',{})
       if delta.get('content') or delta.get('reasoning_content'):
        now=time.perf_counter();first=first or now;last=now
       finish=choice.get('finish_reason') or finish
    end=time.time();assert usage is not None,'Missing server usage';assert usage['completion_tokens']==r['target_output_tokens'],('Output length mismatch',rid,usage,r['target_output_tokens'])
    if r.get('reference_prompt_tokens') is not None:assert usage['prompt_tokens']==r['reference_prompt_tokens'],('Input length mismatch',rid)
    result={'request_id':rid,'root_id':r['root_id'],'start_unix':start,'end_unix':end,'latency_s':time.perf_counter()-perf,'ttft_s':first-perf if first else None,'tpot_ms':1000*(last-first)/(usage['completion_tokens']-1) if first and last and usage['completion_tokens']>1 else None,'usage':usage,'finish_reason':finish,'body_sha256':r['body_sha256']};results.append(result);log('done',**result);ready[rid].set()
   except BaseException as e:log('error',request_id=rid,error=repr(e));raise
   finally:running-=1
  async def tree(root):
   async with sem:
    log('root_begin',root_id=root);tasks=[asyncio.create_task(request(r)) for r in groups[root]]
    try:await asyncio.gather(*tasks)
    except BaseException:
     for task in tasks:task.cancel()
     await asyncio.gather(*tasks,return_exceptions=True);raise
    log('root_done',root_id=root)
  tasks=[asyncio.create_task(tree(root)) for root in manifest['root_order']]
  try:await asyncio.gather(*tasks)
  except BaseException:
   for task in tasks:task.cancel()
   await asyncio.gather(*tasks,return_exceptions=True);raise
 elapsed=max(r['end_unix'] for r in results)-t0;tokens=sum(r['usage']['completion_tokens'] for r in results);assert len(results)==len(rows);assert {r['request_id'] for r in results}==set(byid)
 report={'requests':len(results),'failed':0,'roots':len(groups),'concurrency':concurrency,'peak_inflight_http':peak,'input_tokens':sum(r['usage']['prompt_tokens'] for r in results),'output_tokens':tokens,'duration_s':elapsed,'output_tokens_per_s':tokens/elapsed,'first_send_unix':t0,'last_done_unix':max(r['end_unix'] for r in results),'requests_sha256':manifest['requests_sha256'],'ttft_mean_s':statistics.fmean(r['ttft_s'] for r in results if r['ttft_s'] is not None),'ttft_p95_s':percentile([r['ttft_s'] for r in results if r['ttft_s'] is not None],.95),'tpot_mean_ms':statistics.fmean(r['tpot_ms'] for r in results if r['tpot_ms'] is not None)}
 (out/'summary.json').write_text(json.dumps(report,indent=2));print('FINITE_COMPLETE',json.dumps(report),flush=True)
try:asyncio.run(main())
except BaseException as e:(out/'failure.json').write_text(json.dumps({'error':repr(e),'completed':len(results),'expected':len(rows)},indent=2));raise
finally:events.close()
