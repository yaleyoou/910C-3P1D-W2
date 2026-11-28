#!/usr/bin/env bash
# Fast (<1s) sync of cluster.env, cases.tsv, scripts, services/, tools/, and dhook/
# from P1 to all active remote nodes in cluster.env (1P1D / 2P1D / 3P1D).
# Does NOT copy image.tar.zst, data/, or runs/; requires no rsync or sudo.
set -euo pipefail
cd "$(dirname "$0")"; base=$PWD
source cluster.env

roles=()
hosts=()
if [[ -n ${P2:-} ]]; then roles+=(p2); hosts+=("${SSH_P2:-$P2}"); fi
if [[ -n ${P3:-} ]]; then roles+=(p3); hosts+=("${SSH_P3:-$P3}"); fi
roles+=(d)
hosts+=("${SSH_D:-$D}")

items=()
for f in cluster.env cases.tsv agentx.sh bench.sh case.sh check_assets.sh fetch_assets.sh finish.sh logs.sh node.sh setup_nodes.sh stop.sh sync_code.sh watch.sh services tools dhook; do
  [[ -e $f ]] && items+=("$f")
done

sentinel="$base/.fs-sync-probe.$$"
trap 'rm -f -- "$sentinel"' EXIT
: > "$sentinel"

for ((i=0; i<${#hosts[@]}; i++)); do
  role=${roles[$i]}
  host=${hosts[$i]}
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" test -e "$sentinel" 2>/dev/null; then
    echo "  $role ($host): shared filesystem detected, skipping sync"
  else
    echo "  $role ($host): syncing scripts & config..."
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" "mkdir -p $(printf '%q' "$base")"
    tar -cf - "${items[@]}" | ssh -o BatchMode=yes "$host" "tar -xf - -C $(printf '%q' "$base")"
  fi
done
echo "Code & config sync complete across all active nodes."
