#!/usr/bin/env bash
# Run on a host that can read the old shared model path and SSH to the new hosts.
set -euo pipefail
src=${1:?source model directory}; dest=${2:?absolute destination model directory}; shift 2
for machine in "$@"; do
  ssh "$machine" mkdir -p "$dest"
  rsync -aL --partial --info=progress2 "$src/" "$machine:$dest/"
  rsync -rcnL --itemize-changes "$src/" "$machine:$dest/" > "model-verify-${machine//@/_}.txt"
  test ! -s "model-verify-${machine//@/_}.txt"
done

