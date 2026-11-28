#!/usr/bin/env bash
# Tail the latest log file for any role/service from P1 (handles sudo and remote SSH cleanly).
# Usage: bash logs.sh [p1|p2|p3|d|proxy|master|store-d] [tail_lines]
set -euo pipefail
cd "$(dirname "$0")"; base=$PWD
source cluster.env

target=${1:-d}
lines=${2:-50}

local_tail() {
  local role=$1 file=$2
  sudo bash -c 'f="$(ls -dt "$1"/runs/*/*/*/"$2" 2>/dev/null | head -1)/logs/$3"; [[ -f $f ]] || { echo "Log not found: $f" >&2; exit 1; }; echo "=== $f ==="; tail -n "$4" -f "$f"' _ "$base" "$role" "$file" "$lines"
}

remote_tail() {
  local host=$1 role=$2 file=$3
  [[ -n $host ]] || { echo "Host for $role is empty in cluster.env" >&2; exit 1; }
  ssh -tt "$host" "sudo bash -c 'f=\"\$(ls -dt \"\$1\"/runs/*/*/*/\"\$2\" 2>/dev/null | head -1)/logs/\$3\"; [[ -f \$f ]] || { echo \"Log not found: \$f\" >&2; exit 1; }; echo \"=== \$f ===\"; tail -n \"\$4\" -f \"\$f\"' _ $(printf '%q' "$base") $(printf '%q' "$role") $(printf '%q' "$file") $(printf '%q' "$lines")"
}

case $target in
  p1)      local_tail p1 p.log ;;
  proxy)   local_tail p1 proxy.log ;;
  master)  local_tail p1 master.log ;;
  p2)      remote_tail "${SSH_P2:-${P2:-}}" p2 p.log ;;
  p3)      remote_tail "${SSH_P3:-${P3:-}}" p3 p.log ;;
  d)       remote_tail "${SSH_D:-${D:-}}" d d.log ;;
  store-d) remote_tail "${SSH_D:-${D:-}}" d store-d.log ;;
  *)
    echo "Usage: bash logs.sh [p1|p2|p3|d|proxy|master|store-d] [lines]" >&2
    exit 2
    ;;
esac
