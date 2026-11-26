#!/usr/bin/env bash
# Run on P1 as a normal SSH-enabled user with sudo -n Docker access.
set -euo pipefail
cd "$(dirname "$0")"; base=$PWD
source cluster.env
group=${1:?F1}; trial=${2:?r1}
[[ $group =~ ^[A-Za-z0-9_-]+$ && $trial =~ ^[A-Za-z0-9_-]+$ && $base =~ ^/[A-Za-z0-9_./-]+$ ]]
awk -v g="$group" '$1==g{found=1}END{exit !found}' cases.tsv
roles=(p1 p2 p3 d)
hosts=(localhost "${SSH_P2:-$P2}" "${SSH_P3:-$P3}" "${SSH_D:-$D}")
name=glm52-3p1d
on() {
  local i=$1; shift
  if [[ $i == 0 ]]; then "$@"; else ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "$(printf '%q ' "$@")"; fi
}
ready() {
  local url=$1
  for j in {1..450}; do
    if curl --noproxy '*' -fsS --max-time 5 "$url" >/dev/null 2>&1; then return; fi
    sleep 8
  done
  echo "Readiness timed out: $url" >&2; return 1
}
dlog() { on 3 sudo -n docker exec "$name" cat /run-data/logs/d-blocks.jsonl | sudo -n tee "$1" >/dev/null; }
for i in 0 1 2 3; do
  on "$i" test -s "$base/node.sh"
  on "$i" test -s "$base/cluster.env"
  local_hash=$(sha256sum cluster.env | cut -d' ' -f1)
  host_hash=$(on "$i" sha256sum "$base/cluster.env" | cut -d' ' -f1)
  test "$local_hash" = "$host_hash" || { echo "cluster.env differs on ${roles[$i]}"; exit 1; }
  on "$i" sudo -n docker image inspect "$IMAGE" --format '{{.Id}}'
  if on "$i" sudo -n docker inspect "$name" >/dev/null 2>&1; then echo "Container already exists on ${roles[$i]}";exit 1;fi
done
python3 tools/verify_w2.py data/checkpoints/W2/frozen
for phase in cal perf blocks; do
  pids=()
  for i in 0 1 2 3; do on "$i" sudo -n bash "$base/node.sh" up "${roles[$i]}" "$group" "$phase" "$trial" & pids+=("$!"); done
  status=0; for pid in "${pids[@]}"; do wait "$pid" || status=1; done; test "$status" = 0
  for addr in "$P1:8100" "$P2:8100" "$P3:8100" "$D:8200"; do ready "http://$addr/health"; done
  ready "http://$P1:8300/healthcheck"
  out="$base/runs/$group/$trial/$phase/p1"
  if [[ $phase == blocks ]]; then
    python3 tools/clock_check.py "${hosts[3]}" /dev/stdout | sudo -n tee "$out/clock-before.json" >/dev/null
  fi
  sudo -n docker exec "$name" bash /deploy/bench.sh
  if [[ $phase != perf ]]; then
    dlog "$out/logs/d-blocks.jsonl"
  fi
  if [[ $phase == cal ]]; then sudo -n docker exec "$name" python3 /deploy/tools/check_probe.py /run-data;fi
  if [[ $phase == blocks ]]; then
    python3 tools/clock_check.py "${hosts[3]}" /dev/stdout | sudo -n tee "$out/clock-after.json" >/dev/null
    sudo -n docker exec "$name" bash /deploy/finish.sh
  fi
  for i in 1 2 3 0; do on "$i" sudo -n bash "$base/node.sh" down; done
done
echo "Result: $base/runs/$group/$trial/blocks/p1/result.csv"

