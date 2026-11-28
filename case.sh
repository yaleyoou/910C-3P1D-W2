#!/usr/bin/env bash
# Run on P1 as normal user (e.g. c60040001) or root.
# Supports both NOPASSWD sudo and password sudo (prompts once and keeps in memory).
# Usage:
#   bash case.sh F1 r1
#   bash case.sh F1,F5,F2,F3,F4,EC1,ES1,EJ1 r1
#   bash case.sh all r1
set -euo pipefail
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"; base=$PWD
source cluster.env
groups_arg=${1:?F1 | F1,F5,... | all}; trial=${2:?r1}
[[ $trial =~ ^[A-Za-z0-9_-]+$ && $base =~ ^/[A-Za-z0-9_./-]+$ ]]
if [[ $groups_arg == all ]]; then
  groups=(F1 F5 F2 F3 F4 EC1 ES1 EJ1)
  while read -r extra; do
    skip=0
    for existing in "${groups[@]}"; do [[ $existing == "$extra" ]] && skip=1 && break; done
    ((skip)) || groups+=("$extra")
  done < <(awk '$1 !~ /^#/ && $1 != "case" {print $1}' cases.tsv)
else
  IFS=',' read -r -a groups <<< "$groups_arg"
fi
((${#groups[@]} > 0))
for group in "${groups[@]}"; do
  [[ $group =~ ^[A-Za-z0-9_-]+$ ]]
  awk -v g="$group" '$1 !~ /^#/ && $1 != "case" && $1==g{found=1}END{exit !found}' cases.tsv || {
    echo "Unknown case '$group' in cases.tsv" >&2; exit 1
  }
done

roles=(p1)
hosts=(localhost)
p_addrs=("$P1:8100")
if [[ -n ${P2:-} ]]; then roles+=(p2); hosts+=("${SSH_P2:-$P2}"); p_addrs+=("$P2:8100"); fi
if [[ -n ${P3:-} ]]; then roles+=(p3); hosts+=("${SSH_P3:-$P3}"); p_addrs+=("$P3:8100"); fi
roles+=(d)
hosts+=("${SSH_D:-$D}")
n_nodes=${#roles[@]}
d_idx=$((n_nodes - 1))
name=glm52-3p1d
dkr=(docker)
[[ -z ${DOCKER_HOST:-} ]] || dkr=(docker -H "$DOCKER_HOST")
on() {
  local i=$1; shift
  if [[ $i == 0 ]]; then "$@"; else ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "$(printf '%q ' "$@")"; fi
}
for ((i=1; i<n_nodes; i++)); do
  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" true 2>/dev/null; then
    echo "Passwordless SSH from P1 to ${roles[$i]} (${hosts[$i]}) failed. Configure SSH keys for $(whoami) first." >&2
    exit 1
  fi
done

NEED_SUDO_PASS=0
for ((i=0; i<n_nodes; i++)); do
  if ! on "$i" sh -c '[ "$(id -u)" = 0 ] || sudo -n true' >/dev/null 2>&1; then
    NEED_SUDO_PASS=1
    break
  fi
done
SUDO_PASS=${SUDO_PASS:-}
if ((NEED_SUDO_PASS)) && [[ -z $SUDO_PASS ]]; then
  if [[ -r /dev/tty ]]; then
    read -rsp "Enter sudo password for $(whoami) (shared across $n_nodes nodes): " SUDO_PASS </dev/tty
    echo >/dev/tty
  else
    read -rsp "Enter sudo password for $(whoami): " SUDO_PASS
    echo >&2
  fi
fi

as_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  elif ((NEED_SUDO_PASS == 0)) && sudo -n true 2>/dev/null; then
    sudo -n -- "$@"
  else
    printf '%s\n' "$SUDO_PASS" | sudo -k -S -p '' -- "$@"
  fi
}
on_root() {
  local i=$1; shift
  if [[ $i == 0 ]]; then
    as_root "$@"
  elif ((NEED_SUDO_PASS == 0)); then
    ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "if [ \"\$(id -u)\" = 0 ]; then $(printf '%q ' "$@"); else sudo -n -- $(printf '%q ' "$@"); fi"
  else
    printf '%s\n' "$SUDO_PASS" | ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" \
      "if [ \"\$(id -u)\" = 0 ]; then cat >/dev/null; $(printf '%q ' "$@"); elif sudo -n true 2>/dev/null; then cat >/dev/null; sudo -n -- $(printf '%q ' "$@"); else sudo -k -S -p '' -- $(printf '%q ' "$@"); fi"
  fi
}
write_root() {
  local target=$1 tmp
  if [[ $EUID -eq 0 ]]; then
    cat > "$target"
    return
  fi
  tmp=$(mktemp "$base/.write-root.XXXXXX")
  cat > "$tmp"
  chmod 644 "$tmp"
  as_root mv -f "$tmp" "$target"
}
ready() {
  local url=$1
  for j in {1..450}; do
    if curl --noproxy '*' -fsS --max-time 5 "$url" >/dev/null 2>&1; then return; fi
    sleep 8
  done
  echo "Readiness timed out: $url" >&2; return 1
}
dlog() { on_root "$d_idx" "${dkr[@]}" exec "$name" cat /run-data/logs/d-blocks.jsonl | write_root "$1"; }

for ((i=0; i<n_nodes; i++)); do
  if ! on_root "$i" true; then
    echo "sudo authentication failed on ${roles[$i]} (${hosts[$i]}); aborting immediately to prevent account lockout." >&2
    exit 1
  fi
done
python3 tools/verify_w2.py data/checkpoints/W2/frozen

for group in "${groups[@]}"; do
  echo "=== Starting group $group (trial $trial, topology=$((n_nodes-1))P1D) ==="
  for ((i=0; i<n_nodes; i++)); do
    on "$i" test -s "$base/node.sh"
    on "$i" test -s "$base/cluster.env"
    local_hash=$(sha256sum cluster.env cases.tsv | cut -d' ' -f1)
    host_hash=$(on "$i" sha256sum "$base/cluster.env" "$base/cases.tsv" | cut -d' ' -f1)
    test "$local_hash" = "$host_hash" || { echo "cluster.env or cases.tsv differs on ${roles[$i]}"; exit 1; }
    on_root "$i" bash "$base/node.sh" check "${roles[$i]}" "$group"
    if on_root "$i" "${dkr[@]}" inspect "$name" >/dev/null 2>&1; then echo "Container already exists on ${roles[$i]}";exit 1;fi
  done
  for phase in cal perf blocks; do
    echo "--- [$group/$trial] phase: $phase ---"
    pids=()
    for ((i=0; i<n_nodes; i++)); do on_root "$i" bash "$base/node.sh" up "${roles[$i]}" "$group" "$phase" "$trial" & pids+=("$!"); done
    status=0; for pid in "${pids[@]}"; do wait "$pid" || status=1; done; test "$status" = 0
    for addr in "${p_addrs[@]}" "$D:8200"; do ready "http://$addr/health"; done
    ready "http://$P1:8300/healthcheck"
    out="$base/runs/$group/$trial/$phase/p1"
    if [[ $phase == blocks ]]; then
      python3 tools/clock_check.py "${hosts[$d_idx]}" /dev/stdout | write_root "$out/clock-before.json"
    fi
    as_root "${dkr[@]}" exec "$name" bash /deploy/bench.sh
    if [[ $phase != perf ]]; then
      dlog "$out/logs/d-blocks.jsonl"
    fi
    if [[ $phase == cal ]]; then as_root "${dkr[@]}" exec "$name" python3 /deploy/tools/check_probe.py /run-data;fi
    if [[ $phase == blocks ]]; then
      python3 tools/clock_check.py "${hosts[$d_idx]}" /dev/stdout | write_root "$out/clock-after.json"
      as_root "${dkr[@]}" exec "$name" bash /deploy/finish.sh
    fi
    for ((i=1; i<n_nodes; i++)); do on_root "$i" bash "$base/node.sh" down; done
    on_root 0 bash "$base/node.sh" down
  done
  as_root chmod -R a+rX "$base/runs" 2>/dev/null || true
  done_runs=()
  seen=" "
  for g in F1 F5 F2 F3 F4 EC1 ES1 EJ1 $(awk '$1 !~ /^#/ && $1 != "case" {print $1}' cases.tsv); do
    if [[ $seen != *" $g "* && -s "$base/runs/$g/$trial/blocks/p1/result.csv" ]]; then
      done_runs+=("$base/runs/$g/$trial")
      seen+="$g "
    fi
  done
  if ((${#done_runs[@]} > 0)); then
    python3 tools/result.py "${done_runs[@]}" | write_root "$base/runs/summary_${trial}.csv"
    python3 tools/result.py --markdown "${done_runs[@]}" | write_root "$base/runs/summary_${trial}.md"
  fi
  echo "Result CSV: $base/runs/$group/$trial/blocks/p1/result.csv"
  echo "Result MD:  $base/runs/$group/$trial/blocks/p1/result.md"
  echo "Summary MD: $base/runs/summary_${trial}.md"
done

