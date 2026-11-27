#!/usr/bin/env bash
# Run on P1 as normal user (e.g. c60040001) after unpacking assets and editing cluster.env.
# Prompts once for the shared sudo password, syncs repo + image to P2/P3/D, loads Docker images,
# enables NPU device-share on D, and runs preflight checks across all 4 nodes.
set -euo pipefail
cd "$(dirname "$0")"; base=$PWD
[[ $base =~ ^/[A-Za-z0-9_./-]+$ ]] || { echo "Path must contain only [A-Za-z0-9_./-]: $base" >&2; exit 1; }
source cluster.env
if grep -Eq '^P1=CHANGE_|^P2=CHANGE_|^P3=CHANGE_|^D=CHANGE_' cluster.env; then
  echo "Please fill real IPv4 addresses for P1, P2, P3, D in cluster.env first." >&2
  exit 1
fi
[[ -s image.tar.zst ]] || { echo "Missing image.tar.zst in $base" >&2; exit 1; }
[[ -s data/checkpoints/W2/frozen/manifest.json ]] || { echo "Missing unpacked W2 data; run tar -xzf w2-data.tar.gz -C data first" >&2; exit 1; }
python3 tools/verify_runtime.py

roles=(p1 p2 p3 d)
hosts=(localhost "${SSH_P2:-$P2}" "${SSH_P3:-$P3}" "${SSH_D:-$D}")
on() {
  local i=$1; shift
  if [[ $i == 0 ]]; then "$@"; else ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "$(printf '%q ' "$@")"; fi
}

for i in 1 2 3; do
  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" true 2>/dev/null; then
    echo "Passwordless SSH from P1 to ${roles[$i]} (${hosts[$i]}) failed." >&2
    echo "Configure SSH keys for $(whoami) from P1 to ${hosts[1]}, ${hosts[2]}, ${hosts[3]} first." >&2
    exit 1
  fi
done

NEED_SUDO_PASS=0
for i in 0 1 2 3; do
  if ! on "$i" sh -c '[ "$(id -u)" = 0 ] || sudo -n true' >/dev/null 2>&1; then
    NEED_SUDO_PASS=1
    break
  fi
done
SUDO_PASS=${SUDO_PASS:-}
if ((NEED_SUDO_PASS)) && [[ -z $SUDO_PASS ]]; then
  if [[ -r /dev/tty ]]; then
    read -rsp "Enter sudo password for $(whoami) (shared across 4 nodes): " SUDO_PASS </dev/tty
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

echo "[1/4] Verifying sudo access sequentially on 4 nodes..."
for i in 0 1 2 3; do
  if ! on_root "$i" true; then
    echo "sudo authentication failed on ${roles[$i]} (${hosts[$i]}); aborting immediately to prevent account lockout." >&2
    exit 1
  fi
done

echo "[2/4] Syncing $base to P2, P3, D (if independent disks)..."
sentinel="$base/.fs-probe.$$"
trap 'rm -f -- "$sentinel"' EXIT
: > "$sentinel"
for i in 1 2 3; do
  if on "$i" test -e "$sentinel"; then
    echo "  ${roles[$i]} (${hosts[$i]}): shared filesystem detected, skipping rsync"
  else
    echo "  ${roles[$i]} (${hosts[$i]}): syncing files to $base ..."
    on "$i" mkdir -p "$base"
    rsync -a --info=progress2 --exclude='.git/' --exclude='runs/' --exclude='image.seg-*' --exclude='.fs-probe.*' \
      "$base/" "${hosts[$i]}:$base/"
  fi
done
rm -f -- "$sentinel"

echo "[3/4] Loading Docker image on all 4 nodes (in parallel)..."
pids=()
for i in 0 1 2 3; do
  (
    if on_root "$i" docker image inspect "$IMAGE" >/dev/null 2>&1; then
      echo "  ${roles[$i]}: Docker image already loaded"
    else
      echo "  ${roles[$i]}: loading image.tar.zst ..."
      on_root "$i" bash "$base/node.sh" load
    fi
  ) &
  pids+=("$!")
done
status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
test "$status" = 0

echo "[4/4] Enabling NPU device-share on D and running preflight checks..."
on_root 3 bash -c 'for card in {0..7}; do for chip in 0 1; do printf "y\n" | /usr/local/bin/npu-smi set -t device-share -i "$card" -c "$chip" -d 1 >/dev/null; done; done'
for i in 0 1 2 3; do
  on_root "$i" bash "$base/node.sh" check "${roles[$i]}" F1
done
echo "All 4 nodes verified and ready! Start benchmark inside tmux with: bash case.sh F1 r1 (or: bash case.sh all r1)"
