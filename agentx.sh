#!/usr/bin/env bash
# One-click 3600s zero-delay AgentX replay on the 3P1D cluster.
# Uses the exact same cluster.env, cases.tsv, node.sh, and shared sudo password flow as case.sh.
#
# Usage:
#   bash agentx.sh F1 r1                      # F1, 3600s, c16, data/traces.jsonl
#   bash agentx.sh F1 r1 3600 32              # F1, 3600s, c32
#   bash agentx.sh F1,F5,F2 r1 3600 16 data/traces.jsonl
set -euo pipefail
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"; base=$PWD
source cluster.env

groups_arg=${1:?F1 | F1,F5,... | all}
trial=${2:?r1}
duration=${3:-3600}
concurrency=${4:-16}
traces_arg=${5:-data/traces.jsonl}

[[ $trial =~ ^[A-Za-z0-9_-]+$ && $base =~ ^/[A-Za-z0-9_./-]+$ ]]
[[ $duration =~ ^[0-9]+$ && $concurrency =~ ^[0-9]+$ ]]

# Resolve trace path under repo root so it is visible at /deploy/... inside container
traces_abs=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$traces_arg")
base_abs=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$base")
[[ -s "$traces_abs" ]] || { echo "Trace file not found: $traces_arg" >&2; exit 1; }
if [[ $traces_abs != "$base_abs/"* ]]; then
  echo "Trace file must reside inside $base (so Docker mounts it under /deploy), e.g.:" >&2
  echo "  mv '$traces_abs' '$base/data/traces.jsonl'" >&2
  exit 1
fi
traces_in_container="/deploy/${traces_abs#"$base_abs/"}"

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
name=glm52-3p1d
dkr=(docker)
[[ -z ${DOCKER_HOST:-} ]] || dkr=(docker -H "$DOCKER_HOST")

on() {
  local i=$1; shift
  if [[ $i == 0 ]]; then "$@"; else ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "$(printf '%q ' "$@")"; fi
}
for ((i=1; i<n_nodes; i++)); do
  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" true 2>/dev/null; then
    echo "Passwordless SSH from P1 to ${roles[$i]} (${hosts[$i]}) failed." >&2
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

for ((i=0; i<n_nodes; i++)); do
  if ! on_root "$i" true; then
    echo "sudo authentication failed on ${roles[$i]} (${hosts[$i]}); aborting." >&2
    exit 1
  fi
done

run_tag="agentx_${trial}_d${duration}_c${concurrency}"
for group in "${groups[@]}"; do
  echo "=== Starting AgentX group $group (tag=$run_tag, topology=$((n_nodes-1))P1D, duration=${duration}s, c=${concurrency}) ==="
  for ((i=0; i<n_nodes; i++)); do
    on "$i" test -s "$base/node.sh"
    on "$i" test -s "$base/cluster.env"
    local_hash=$(sha256sum cluster.env cases.tsv | cut -d' ' -f1)
    host_hash=$(on "$i" sha256sum "$base/cluster.env" "$base/cases.tsv" | cut -d' ' -f1)
    test "$local_hash" = "$host_hash" || { echo "cluster.env or cases.tsv differs on ${roles[$i]}"; exit 1; }
    on_root "$i" bash "$base/node.sh" check "${roles[$i]}" "$group"
    if on_root "$i" "${dkr[@]}" inspect "$name" >/dev/null 2>&1; then echo "Container already exists on ${roles[$i]}"; exit 1; fi
  done

  pids=()
  for ((i=0; i<n_nodes; i++)); do on_root "$i" bash "$base/node.sh" up "${roles[$i]}" "$group" perf "$run_tag" & pids+=("$!"); done
  status=0; for pid in "${pids[@]}"; do wait "$pid" || status=1; done; test "$status" = 0

  for addr in "${p_addrs[@]}" "$D:8200"; do ready "http://$addr/health"; done
  ready "http://$P1:8300/healthcheck"

  as_root "${dkr[@]}" exec \
    -e "AGENTX_TRACES=$traces_in_container" \
    -e "AGENTX_DURATION=$duration" \
    -e "AGENTX_CONCURRENCY=$concurrency" \
    -e "AGENTX_HARD_STOP=${AGENTX_HARD_STOP:-0}" \
    "$name" python3 /deploy/tools/bench_agentx.py

  for ((i=1; i<n_nodes; i++)); do on_root "$i" bash "$base/node.sh" down; done
  on_root 0 bash "$base/node.sh" down

  as_root chmod -R a+rX "$base/runs" 2>/dev/null || true
  done_runs=()
  seen=" "
  for g in F1 F5 F2 F3 F4 EC1 ES1 EJ1 $(awk '$1 !~ /^#/ && $1 != "case" {print $1}' cases.tsv); do
    if [[ $seen != *" $g "* && -s "$base/runs/$g/$run_tag/perf/p1/result.csv" ]]; then
      done_runs+=("$base/runs/$g/$run_tag/perf/p1")
      seen+="$g "
    fi
  done
  if ((${#done_runs[@]} > 0)); then
    python3 tools/summarize_agentx.py "${done_runs[@]}" | write_root "$base/runs/summary_${run_tag}.csv"
    python3 tools/summarize_agentx.py --markdown "${done_runs[@]}" | write_root "$base/runs/summary_${run_tag}.md"
  fi
  echo "Result MD:  $base/runs/$group/$run_tag/perf/p1/result.md"
  echo "Window MD:  $base/runs/$group/$run_tag/perf/p1/result-window.md"
  echo "Summary MD: $base/runs/summary_${run_tag}.md"
done
