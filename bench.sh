#!/usr/bin/env bash
# Run on P1 inside the experiment container; /deploy is mounted read-only.
set -eo pipefail
cd /deploy
[[ $ROLE == p1 ]]
export no_proxy='*' NO_PROXY='*'
mkdir -p /run-data/logs
for endpoint in "$P1:8100" "$P2:8100" "$P3:8100" "$D:8200"; do
  curl --noproxy '*' -fsS "http://$endpoint/health" -o /dev/null
done
curl --noproxy '*' -fsS "http://$P1:8300/healthcheck" -o /dev/null
python3 -c 'import os,json;open("/run-data/params.json","w").write(json.dumps({"case":os.environ["CASE"],"ps":int(os.environ["P_STORE"]),"ds":int(os.environ["D_STORE"]),"sparse":int(os.environ["SPARSE"]),"hosts":[os.environ[k] for k in ["P1","P2","P3","D"]]}))'
if [[ $PHASE == cal ]]; then
  pids=()
  trap 'for x in "${pids[@]}"; do kill "$x" 2>/dev/null || true; done' EXIT
  port=8310
  for host in "$P1" "$P2" "$P3"; do
    port=$((port+1))
    python3 tools/proxy_reliable.py --host "$P1" --port "$port" --prefiller-hosts "$host" --prefiller-ports 8100 --decoder-hosts "$D" --decoder-ports 8200 --max-retries 1 > "/run-data/logs/proxy-$port.log" 2>&1 &
    pids+=("$!")
    for j in {1..60}; do curl -fsS "http://$P1:$port/healthcheck" -o /dev/null && break; sleep 2; done
  done
  python3 tools/calibrate.py
else
  python3 tools/bench.py
fi

