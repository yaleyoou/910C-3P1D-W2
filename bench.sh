#!/usr/bin/env bash
# Run on P1 inside the experiment container; /deploy is mounted read-only.
set -eo pipefail
cd /deploy
[[ $ROLE == p1 ]]
export no_proxy='*' NO_PROXY='*'
mkdir -p /run-data/logs
p_hosts=()
for h in "${P1:-}" "${P2:-}" "${P3:-}"; do
  [[ -n $h ]] && p_hosts+=("$h")
done
for h in "${p_hosts[@]}"; do
  curl --noproxy '*' -fsS "http://$h:8100/health" -o /dev/null
done
curl --noproxy '*' -fsS "http://$D:8200/health" -o /dev/null
curl --noproxy '*' -fsS "http://$P1:8300/healthcheck" -o /dev/null
python3 - <<'PY'
import os, sys, time
sys.path.insert(0, '/deploy/tools')
from telemetry import scrape
roles = [r for r in ('P1', 'P2', 'P3') if os.environ.get(r)] + ['D']
deadline = time.time() + 300
last_err = None
while time.time() < deadline:
    try:
        m = scrape('master')
        ok = True
        for role in roles:
            exp = int(os.environ['D_STORE' if role == 'D' else 'P_STORE'])
            got = m[role + '_segment_total_capacity_bytes']
            if abs(got - exp) >= 1e-6:
                ok = False
                last_err = f"{role}: registered {got:.3f} GiB != expected {exp} GiB"
                break
        if ok:
            sys.exit(0)
    except Exception as exc:
        last_err = str(exc)
    time.sleep(3)
sys.exit(f"Timed out waiting for Mooncake Store segments to register: {last_err}")
PY
python3 -c 'import os,json;ps=[k for k in ["P1","P2","P3"] if os.environ.get(k)];open("/run-data/params.json","w").write(json.dumps({"case":os.environ["CASE"],"ps":int(os.environ["P_STORE"]),"p_count":len(ps),"ds":int(os.environ["D_STORE"]),"sparse":int(os.environ["SPARSE"]),"hosts":[os.environ[k] for k in ps+["D"]]}))'
if [[ $PHASE == cal ]]; then
  pids=()
  trap 'for x in "${pids[@]}"; do kill "$x" 2>/dev/null || true; done' EXIT
  port=8310
  for host in "${p_hosts[@]}"; do
    port=$((port+1))
    python3 tools/proxy_reliable.py --host "$P1" --port "$port" --prefiller-hosts "$host" --prefiller-ports 8100 --decoder-hosts "$D" --decoder-ports 8200 --max-retries 1 > "/run-data/logs/proxy-$port.log" 2>&1 &
    pids+=("$!")
    for j in {1..60}; do curl -fsS "http://$P1:$port/healthcheck" -o /dev/null && break; sleep 2; done
  done
  python3 tools/calibrate.py
else
  python3 tools/bench.py
fi

