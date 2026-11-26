#!/usr/bin/env bash
set -e

source /usr/local/Ascend/ascend-toolkit/set_env.sh
mkdir -p /run-data/logs
exec > /run-data/logs/master.log 2>&1

exec mooncake_master \
    --port 50088 \
    --metrics_port 50089 \
    --enable_offload=false \
    --eviction_high_watermark_ratio 0.9 \
    --eviction_ratio 0.1 \
    --default_kv_lease_ttl 60000 \
    --client_ttl 120
