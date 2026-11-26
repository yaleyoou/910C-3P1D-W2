#!/usr/bin/env bash
set -e
export TRITON_CACHE_DIR=/root/.cache/triton

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
unset ASCEND_ENABLE_USE_FABRIC_MEM ASCEND_USE_ASYNC_TRANSFER
export VLLM_HOST_IP=$D HCCL_IF_IP=$D
export GLOO_SOCKET_IFNAME=$IFACE TP_SOCKET_IFNAME=$IFACE HCCL_SOCKET_IFNAME=$IFACE
export HCCL_IF_BASE_PORT=54000 ASCEND_BASE_PORT=22000
export ASCEND_CONNECT_TIMEOUT=10000 ASCEND_TRANSFER_TIMEOUT=10000
export HCCL_BUFFSIZE=200 OMP_NUM_THREADS=1 OMP_PROC_BIND=false
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export PYTHONHASHSEED=0 ACL_OP_INIT_MODE=1
export no_proxy='*' NO_PROXY='*'

mkdir -p /run-data/logs
source /deploy/upgrades/memfabric/enable_4t.sh
exec > /run-data/logs/d.log 2>&1

if [[ "$PHASE" != perf ]]; then export PYTHONPATH=/deploy/dhook:$PYTHONPATH; fi
exec vllm serve /models/glm-q \
    --host $D \
    --port 8200 \
    --served-model-name glm-5 \
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
    --max-num-seqs 32 \
    --watermark 0.2 \
    --max-num-batched-tokens 2048 \
    --block-size 128 \
    --gpu-memory-utilization 0.90 \
    --enable-chunked-prefill \
    --enable-prefix-caching \
    --enable-logging-iteration-details \
    --generation-config vllm \
    --trust-remote-code \
    --seed 0 \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY","cudagraph_capture_sizes":[1, 2, 4, 8, 16, 32]}' \
    --additional-config '{
        "ascend_log_path": "/dev/shm/glm52-ascend-logs",
        "sparse_kv_offload_config": {
            "enabled": true,
            "use_fused_overlap": true,
            "topk_buffer_size": 4096,
            "dram_size_per_dp_GB": '"$SPARSE"',
            "keep_device_kv_cache": false
        }
    }' \
    --kv-transfer-config '{
        "kv_connector": "SfaRemoteD2HConnector",
        "kv_role": "kv_consumer",
        "kv_port": 41000,
        "kv_connector_extra_config": {
            "transfer_backend": "memfabric",
            "memfabric_transfer_protocol": "sdma"
        }
    }'
