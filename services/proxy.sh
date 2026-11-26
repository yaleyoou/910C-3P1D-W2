#!/usr/bin/env bash
set -e

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export no_proxy='*' NO_PROXY='*'
mkdir -p /run-data/logs
exec > /run-data/logs/proxy.log 2>&1

exec python3 /deploy/tools/proxy_reliable.py \
    --host $NODE_IP \
    --port 8300 \
    --prefiller-hosts "$P1" "$P2" "$P3" \
    --prefiller-ports 8100 8100 8100 \
    --decoder-hosts $D \
    --decoder-ports 8200 \
    --max-retries 1
