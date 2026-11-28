#!/usr/bin/env bash
set -e
export TRITON_CACHE_DIR=/root/.cache/triton

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
unset ASCEND_ENABLE_USE_FABRIC_MEM ASCEND_USE_ASYNC_TRANSFER
export VLLM_HOST_IP=$NODE_IP HCCL_IF_IP=$NODE_IP
export GLOO_SOCKET_IFNAME=$IFACE TP_SOCKET_IFNAME=$IFACE HCCL_SOCKET_IFNAME=$IFACE
export HCCL_IF_BASE_PORT=52000 ASCEND_BASE_PORT=20000
export ASCEND_CONNECT_TIMEOUT=10000 ASCEND_TRANSFER_TIMEOUT=10000
export HCCL_BUFFSIZE=200 OMP_NUM_THREADS=1 OMP_PROC_BIND=false
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export PYTHONHASHSEED=0 ACL_OP_INIT_MODE=1
export ASCEND_LOCAL_COMM_RES='' ASCEND_BUFFER_POOL=4:8 ASCEND_GLOBAL_RESOURCE_CONFIG='{}'
export MOONCAKE_CONFIG_PATH=/run-data/mooncake.json
export no_proxy='*' NO_PROXY='*'

# Per-node GiB divided among TP16 workers.
cat > /run-data/mooncake.json <<EOF
{
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "master_server_address": "$P1:50088",
  "global_segment_size": $((P_STORE * 1024 * 1024 * 1024 / 16)),
  "local_buffer_size": 0,
  "preferred_segment": false,
  "prefer_alloc_in_same_node": true
}
EOF

mkdir -p /run-data/logs
source /deploy/upgrades/memfabric/enable_4t.sh
exec > /run-data/logs/p.log 2>&1

exec vllm serve /models/glm-q \
    --host $NODE_IP \
    --port 8100 \
    --served-model-name glm-5 \
    --additional-config '{"ascend_log_path": "/dev/shm/glm52-ascend-logs"}' \
    --tensor-parallel-size 16 \
    --data-parallel-size 1 \
    --pipeline-parallel-size 1 \
    --enable-expert-parallel \
    --distributed-executor-backend mp \
    --quantization ascend \
    --dtype bfloat16 \
    --kv-cache-dtype bfloat16 \
    --attention-config '{"indexer_kv_dtype":"int8"}' \
    --max-model-len 256000 \
    --max-num-seqs 16 \
    --max-num-batched-tokens 4096 \
    --block-size 128 \
    --gpu-memory-utilization 0.90 \
    --enable-chunked-prefill \
    --enable-prefix-caching \
    --enable-logging-iteration-details \
    --enforce-eager \
    --generation-config vllm \
    --trust-remote-code \
    --seed 0 \
    --kv-transfer-config '{
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "engine_id": "prefill-'"$ROLE"'",
        "kv_connector_extra_config": {
            "connectors": [
                {
                    "kv_connector": "SfaRemoteD2HConnector",
                    "kv_role": "kv_producer",
                    "kv_port": 27000,
                    "kv_connector_extra_config": {
                        "transfer_backend": "memfabric",
                        "memfabric_transfer_protocol": "sdma"
                    }
                },
                {
                    "kv_connector": "AscendStoreConnector",
                    "kv_role": "kv_both",
                    "kv_connector_extra_config": {
                        "backend": "mooncake",
                        "lookup_rpc_port": "p"
                    }
                }
            ]
        }
    }'
