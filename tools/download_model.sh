#!/usr/bin/env bash
set -euo pipefail
dest=${1:-/home/youxingye/youxingye_data/models/GLM-5.2-w4a8c8}
args=(--model Eco-Tech/GLM-5.2-w4a8c8 --local_dir "$dest")
if [[ -n ${2:-} ]]; then args+=(--revision "$2"); fi
modelscope download "${args[@]}"
