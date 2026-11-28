#!/usr/bin/env bash
# Stop any running case.sh / agentx.sh driver on P1 and remove only this experiment's
# glm52-3p1d container on all active nodes configured in cluster.env.
# Prompts once for shared sudo password if needed.
set -euo pipefail
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"; base=$PWD
source cluster.env

pkill -f "bash .*(case|agentx)\.sh" 2>/dev/null || true

roles=(p1)
hosts=(localhost)
if [[ -n ${P2:-} ]]; then roles+=(p2); hosts+=("${SSH_P2:-$P2}"); fi
if [[ -n ${P3:-} ]]; then roles+=(p3); hosts+=("${SSH_P3:-$P3}"); fi
roles+=(d)
hosts+=("${SSH_D:-$D}")
n_nodes=${#roles[@]}

dkr=(docker)
[[ -z ${DOCKER_HOST:-} ]] || dkr=(docker -H "$DOCKER_HOST")

on() {
  local i=$1; shift
  if [[ $i == 0 ]]; then "$@"; else ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "$(printf '%q ' "$@")"; fi
}

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
    echo
  fi
fi

on_root() {
  local i=$1; shift
  if [[ $i == 0 ]]; then
    if [[ $EUID -eq 0 ]]; then "$@";
    elif ((NEED_SUDO_PASS == 0)) || sudo -n true 2>/dev/null; then sudo -n -- "$@";
    else printf '%s\n' "$SUDO_PASS" | sudo -k -S -p '' -- "$@"; fi
  elif ((NEED_SUDO_PASS == 0)); then
    ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" "if [ \"\$(id -u)\" = 0 ]; then $(printf '%q ' "$@"); else sudo -n -- $(printf '%q ' "$@"); fi"
  else
    printf '%s\n' "$SUDO_PASS" | ssh -o BatchMode=yes -o ConnectTimeout=8 "${hosts[$i]}" \
      "if [ \"\$(id -u)\" = 0 ]; then cat >/dev/null; $(printf '%q ' "$@"); elif sudo -n true 2>/dev/null; then cat >/dev/null; sudo -n -- $(printf '%q ' "$@"); else sudo -k -S -p '' -- $(printf '%q ' "$@"); fi"
  fi
}

for ((i=0; i<n_nodes; i++)); do
  echo "Stopping glm52-3p1d on ${roles[$i]} (${hosts[$i]})..."
  on_root "$i" bash -c '
    [[ -z ${1:-} ]] || export DOCKER_HOST=$1
    if docker inspect glm52-3p1d >/dev/null 2>&1; then
      lbl=$(docker inspect -f "{{index .Config.Labels \"experiment\"}}" glm52-3p1d 2>/dev/null || true)
      if [[ "$lbl" == glm52-3p1d ]]; then
        docker rm -f glm52-3p1d >/dev/null && echo "  removed glm52-3p1d"
      else
        echo "  skipped glm52-3p1d (unexpected label: $lbl)" >&2
      fi
    else
      echo "  not running"
    fi
  ' _ "${DOCKER_HOST:-}"
done
echo "All active nodes stopped."
