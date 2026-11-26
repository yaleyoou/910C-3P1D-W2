import json
import os
import sys
import time

import torch.multiprocessing as mp

def donor(rank: int, ip: str, node_gib: int, master: str) -> None:
    os.environ['ASCEND_GLOBAL_RESOURCE_CONFIG'] = json.dumps({
        'store': {'comm_resource_config.listen_port': 36200 + rank}})
    import torch_npu
    from mooncake.store import MooncakeDistributedStore

    torch_npu.npu.set_device(rank)
    store = MooncakeDistributedStore()
    assert store.setup(local_hostname=ip, metadata_server='P2PHANDSHAKE',
                global_segment_size=node_gib * 2**30 // 16, local_buffer_size=0,
                protocol='ascend', rdma_devices='',
                master_server_addr=master) == 0
    print(f'STORE_READY rank={rank} bytes={node_gib * 2**30 // 16}', flush=True)
    while True:
        time.sleep(30)


if __name__ == '__main__':
    mp.spawn(donor, args=(sys.argv[1], int(sys.argv[2]), sys.argv[3]), nprocs=16, join=True)
