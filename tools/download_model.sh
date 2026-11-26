#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../cluster.env"
dest=${1:-$MODEL_DIR}
args=(--model Eco-Tech/GLM-5.2-w4a8c8 --local_dir "$dest")
if [[ -n ${2:-} ]]; then args+=(--revision "$2"); fi
modelscope download "${args[@]}"
