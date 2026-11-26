# 最新完成状态：2026-11-16

256GiB实机验证与全部原goal回放已完成。最终证据见../../results/final-analysis/patch-evidence-current-audit.json和完整报告；以下为早期有日期验证记录，原先“实机待完成”等状态已由最终审计取代。

# MemFabric 4 TiB offload window: hardware validation

Validated on node23, TP16, Ascend 910C, 2026-11-15. This validates the isolated shared-pool path; full model performance experiments remain pending.

## Change and provenance
Base source commit137c52fb9f4275c80acc51d73e09928aee86b2d5 matches the official1.3.1 wheel. Isolated candidate: v131-4t. The patch changes HYBM_OFFLOAD_VA_SIZE from2TiB to4TiB and retains the reservation-limit check. Original system installation is unchanged. See offload-4t.patch and patch-artifacts/manifest.json for exact changes and library hashes.

## Evidence
-137 related unit tests passed, including TP16 reservation256GiB accepted and257GiB rejected; patch-ut.log and test XML.
-128GiB: rank0 physically allocates the fullpool; other15ranks map it. All16ranks passed three rounds of sparse-copy numerical checks and exited0. patch-rw-128.log.
-256GiB: all16ranks passed the same checks and exited0; outer TEST_EXIT_CODE=0. patch-rw-256-timeout600.log.
-The second checked buffer is138512699392bytes from the first (129GiB+4096bytes), beyond the old128GiB boundary.
-256GiB host MemAvailable before/after initialization:2032123864/1762891680KiB. This is an observed whole-host change, not an allocator-exact measurement.

## Initialization timeout
The first256GiB attempt failed because nonowner ranks timed out in AllGather after120seconds while rank0 remained inside driver mapping/access setup. Preserve patch-rw-256-timeout120.log, patch-rw-256-init-kernel-stack.txt and patch-rw-256-init-meminfo.txt. Raising the existing MF_GROUP_JOIN_MAX_TIMEOUT to600seconds allowed completion; no address validation was removed. The successful retry is stronger evidence than an assumption that allocation alone succeeded.

## Scope and next stage
The test allocates the fullpool but checks two small regions over three updates on all16ranks; it is not exhaustive memory testing or evidence of end-to-end model stability. Run identical candidate libraries and timeout600 on128/s16/c16,256/s16/c16,256/s32/c16,256/s32/c32, each AgentX3000seconds plus full drain. Keep PStore64GiB,DStore128GiB fixed for this phase. Fixed-total-budget sparse/Store allocation comparisons follow only after actual capacities and demand are measured.
