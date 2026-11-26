#!/usr/bin/env bash
set -e

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
unset ASCEND_ENABLE_USE_FABRIC_MEM ASCEND_USE_ASYNC_TRANSFER
export VLLM_HOST_IP=$D HCCL_IF_IP=$D HCCL_SOCKET_IFNAME=$IFACE
export ASCEND_BASE_PORT=24000 ASCEND_LOCAL_COMM_RES='' ASCEND_BUFFER_POOL=4:8
export OMP_NUM_THREADS=1 OMP_PROC_BIND=false
export no_proxy='*' NO_PROXY='*'
mkdir -p /run-data/logs
source /deploy/upgrades/memfabric/enable_4t.sh
exec > /run-data/logs/store-d.log 2>&1

exec python3 /deploy/services/store.py "$D" "$D_STORE" $P1:50088
