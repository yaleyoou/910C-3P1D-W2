#!/usr/bin/env python3
"""Analyze GLM-5.2 Indexer K size and required Main KV (Sparse KV Offload DRAM) from d.log.

Usage:
  python3 tools/analyze_indexer_kv.py                          # Auto-read latest d.log from D node via cluster.env
  python3 tools/analyze_indexer_kv.py /path/to/d.log           # Analyze a specific local d.log
  python3 tools/analyze_indexer_kv.py runs/F1/r6/perf/d/logs/d.log --config /home/caofei/GLM-5.2-w4a8/config.json
"""
from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def load_cluster_env(repo: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    p = repo / "cluster.env"
    if not p.is_file():
        return env
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def read_remote_latest_dlog(repo: Path, env: dict[str, str], run_filter: str | None = None) -> tuple[str, str]:
    d_host = env.get("SSH_D") or env.get("D")
    if not d_host:
        raise SystemExit("cluster.env missing D / SSH_D, please pass a local d.log path.")
    pattern = f"{repo}/runs/{run_filter}/d/logs/d.log" if run_filter else f"{repo}/runs/*/*/*/d/logs/d.log"
    inner = (
        f'f=$(ls -t {pattern} 2>/dev/null | head -1); '
        f'[[ -f "$f" ]] || {{ echo "NOT_FOUND: {pattern}" >&2; exit 1; }}; '
        f'echo "__LOG_PATH__=$f"; '
        f'grep -E "Available KV cache memory|Sparse KV offload|SFAPDRD2H D-side|SFAPDRD2H D DP topology|'
        f'dram_size_per_dp_GB|data_parallel_size|tensor_parallel_size|NPU KV cache usage" "$f" || true'
    )
    # Try without sudo first (works when runs/ is world-readable), then sudo -n, then getpass + sudo -S
    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", d_host,
         f"f=$(ls -t {pattern} 2>/dev/null | head -1); test -r \"$f\""],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", d_host, f"bash -c {shlex.quote(inner)}"],
                              capture_output=True, text=True)
    else:
        chk = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", d_host,
             "[ \"$(id -u)\" = 0 ] || sudo -n true"],
            capture_output=True, text=True,
        )
        if chk.returncode == 0:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", d_host, f"sudo -n -- bash -c {shlex.quote(inner)}"],
                capture_output=True, text=True,
            )
        else:
            pw = os.environ.get("SUDO_PASS") or getpass.getpass(f"Enter sudo password for {d_host}: ")
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", d_host, f"sudo -k -S -p '' -- bash -c {shlex.quote(inner)}"],
                input=pw + "\n", capture_output=True, text=True,
            )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"Failed to read d.log from {d_host}")
    text = proc.stdout.replace("\r\n", "\n")
    m = re.search(r"__LOG_PATH__=(.+)", text)
    src = f"{d_host}:{m.group(1).strip()}" if m else f"{d_host}:d.log"
    return src, text


def parse_model_config(config_path: Path | None) -> dict:
    # Verified GLM-5.2-w4a8 defaults
    info = {
        "num_hidden_layers": 78,
        "kv_lora_rank": 512,
        "qk_rope_head_dim": 64,
        "index_head_dim": 128,
        "index_n_heads": 32,
        "index_topk": 2048,
        "indexer_layers_from_config": 21,
        "config_loaded": False,
    }
    if config_path and config_path.is_file():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        info["config_loaded"] = True
        for k in ("num_hidden_layers", "kv_lora_rank", "qk_rope_head_dim", "index_head_dim", "index_n_heads", "index_topk"):
            if k in cfg and cfg[k] is not None:
                info[k] = int(cfg[k])
        if isinstance(cfg.get("indexer_types"), list):
            full_cnt = sum(1 for x in cfg["indexer_types"] if x == "full")
            if full_cnt > 0:
                info["indexer_layers_from_config"] = full_cnt
    return info


def analyze_text(text: str, model: dict, block_size: int = 128, watermark: float = 0.2) -> dict:
    # 1. Topology & configured params
    dp_size = 1
    tp_size = 16
    m_topo = re.search(r"data_parallel_size=(\d+).*?tp_size=(\d+)", text)
    if m_topo:
        dp_size = int(m_topo.group(1))
        tp_size = int(m_topo.group(2))
    else:
        m_dp = re.search(r"--data-parallel-size\s+(\d+)|data_parallel_size[=:]\s*(\d+)", text)
        m_tp = re.search(r"--tensor-parallel-size\s+(\d+)|tensor_parallel_size[=:]\s*(\d+)", text)
        if m_dp:
            dp_size = int(m_dp.group(1) or m_dp.group(2))
        if m_tp:
            tp_size = int(m_tp.group(1) or m_tp.group(2))

    m_bs = re.search(r"--block-size\s+(\d+)|block_size[=:]\s*(\d+)", text)
    if m_bs:
        block_size = int(m_bs.group(1) or m_bs.group(2))
    m_wm = re.search(r"--watermark\s+([0-9.]+)", text)
    if m_wm:
        watermark = float(m_wm.group(1))

    m_cfg_dram = re.search(r"['\"]?dram_size_per_dp_GB['\"]?\s*[:=]\s*(\d+)", text)
    cfg_dram_gb = int(m_cfg_dram.group(1)) if m_cfg_dram else None

    # 2. Registered layer counts (indexer + main)
    m_reg = re.search(r"SFAPDRD2H D-side registered[^:]*:\s*(\d+)\s+indexer\s*\+\s*(\d+)\s+TP-shared main layers", text)
    indexer_layers = int(m_reg.group(1)) if m_reg else model["indexer_layers_from_config"]
    main_layers = int(m_reg.group(2)) if m_reg else model["num_hidden_layers"]

    # 3. Available HBM KV cache memory (collect per DP / per worker, deduplicate)
    avail_vals = [float(x) for x in re.findall(r"Available KV cache memory:\s*([0-9.]+)\s*GiB", text)]
    avail_hbm_gib = min(avail_vals) if avail_vals else None

    # 4. Sparse KV offload memory plan line (exact vLLM-Ascend numbers)
    plans = []
    plan_re = re.compile(
        r"Sparse KV offload memory plan:\s*"
        r"npu_limit=(\d+)\s+blocks,\s*"
        r"dram_limit=(\d+)\s+blocks,\s*"
        r"workload_limit=(\d+)\s+blocks,\s*"
        r"final=(\d+)\s+blocks\s*\(([^)]+)\),\s*"
        r"planner=([0-9.]+)\s+GiB,\s*"
        r"host=([0-9.]+)\s+GiB,\s*"
        r"device=([0-9.]+)\s+GiB,\s*"
        r"alignment_reserve=([0-9.]+)\s+GiB"
    )
    for m in plan_re.finditer(text):
        plans.append({
            "npu_limit": int(m.group(1)),
            "dram_limit": int(m.group(2)),
            "workload_limit": int(m.group(3)),
            "final": int(m.group(4)),
            "limiter": m.group(5).strip(),
            "planner_gib": float(m.group(6)),
            "host_gib": float(m.group(7)),
            "device_gib": float(m.group(8)),
            "align_gib": float(m.group(9)),
        })
    plan = plans[0] if plans else None

    # 5. Live Iteration stats from d.log
    iters = re.findall(
        r"Iteration\(\d+\):.*?(\d+)\s+generation requests.*?NPU KV cache usage:\s*([0-9.]+)%",
        text,
    )
    kv_pcts = [float(p) for _, p in iters]
    gen_reqs = [int(r) for r, _ in iters]

    # 6. Exact per-token / per-block sizing
    # GLM-5.2 Main KV: num_main_layers * (kv_lora_rank + qk_rope_head_dim) * 2 bytes (BF16)
    main_b_per_tok = main_layers * (model["kv_lora_rank"] + model["qk_rope_head_dim"]) * 2
    main_b_per_block = main_b_per_tok * block_size

    # Indexer K: infer exact bytes/block from log if available; fallback to Ascend INT8 aligned stride (256 B/layer/tok)
    indexer_b_per_block_obs = None
    if plan and plan["final"] > 0 and plan["device_gib"] > 0:
        indexer_b_per_block_obs = (plan["device_gib"] * (1024 ** 3)) / plan["final"]
    elif avail_hbm_gib and plan and plan["npu_limit"] > 0:
        indexer_b_per_block_obs = (avail_hbm_gib * (1024 ** 3)) / plan["npu_limit"]

    # Snap to nearest 256B*block_size or exact observed value
    if indexer_b_per_block_obs is not None:
        raw_per_layer_tok = indexer_b_per_block_obs / block_size / indexer_layers
        # Check if within 1% of an integer power/multiple of 16 bytes (Ascend 256B alignment = 128B int8 + 128B aligned scale/pad)
        snapped = round(raw_per_layer_tok / 16.0) * 16
        if snapped > 0 and abs(raw_per_layer_tok - snapped) / snapped < 0.01:
            indexer_b_per_layer_tok = snapped
        else:
            indexer_b_per_layer_tok = raw_per_layer_tok
        indexer_source = "日志实测反推"
    else:
        # Fallback: 256 B/layer/tok (verified on Ascend 910C GLM-5.2 int8 indexer_kv_dtype)
        indexer_b_per_layer_tok = 256
        indexer_source = "GLM-5.2 Ascend INT8 实测常量（未在日志中找到 memory plan 行）"

    indexer_b_per_tok = indexer_layers * indexer_b_per_layer_tok
    indexer_b_per_block = indexer_b_per_tok * block_size
    ratio = main_b_per_block / indexer_b_per_block

    # 7. Calculate how much Main KV is needed to saturate the NPU Indexer K
    if plan:
        npu_blocks = plan["npu_limit"]
        final_blocks = plan["final"]
        idx_cap_gib_per_dp = (npu_blocks * indexer_b_per_block) / (1024 ** 3)
        align_ratio = (plan["align_gib"] / plan["host_gib"]) if plan["host_gib"] > 0 else 0.003
    elif avail_hbm_gib:
        npu_blocks = int((avail_hbm_gib * (1024 ** 3)) // indexer_b_per_block)
        final_blocks = int((cfg_dram_gb * (1024 ** 3) * 0.997) // main_b_per_block) if cfg_dram_gb else npu_blocks
        idx_cap_gib_per_dp = avail_hbm_gib
        align_ratio = 0.003
    else:
        npu_blocks = None
        final_blocks = None
        idx_cap_gib_per_dp = None
        align_ratio = 0.003

    full_main_pure_gib_per_dp = (npu_blocks * main_b_per_block) / (1024 ** 3) if npu_blocks else None
    full_main_cfg_gib_per_dp = (
        math.ceil(full_main_pure_gib_per_dp * (1.0 + align_ratio))
        if full_main_pure_gib_per_dp is not None
        else None
    )
    wm_active_main_gib_per_dp = (
        full_main_pure_gib_per_dp * (1.0 - watermark)
        if full_main_pure_gib_per_dp is not None
        else None
    )

    return {
        "dp_size": dp_size,
        "tp_size": tp_size,
        "block_size": block_size,
        "watermark": watermark,
        "cfg_dram_gb": cfg_dram_gb,
        "main_layers": main_layers,
        "indexer_layers": indexer_layers,
        "main_b_per_tok": main_b_per_tok,
        "main_b_per_block": main_b_per_block,
        "indexer_b_per_layer_tok": indexer_b_per_layer_tok,
        "indexer_b_per_tok": indexer_b_per_tok,
        "indexer_b_per_block": indexer_b_per_block,
        "indexer_source": indexer_source,
        "ratio": ratio,
        "avail_hbm_gib": avail_hbm_gib,
        "plan": plan,
        "npu_blocks": npu_blocks,
        "final_blocks": final_blocks,
        "idx_cap_gib_per_dp": idx_cap_gib_per_dp,
        "full_main_pure_gib_per_dp": full_main_pure_gib_per_dp,
        "full_main_cfg_gib_per_dp": full_main_cfg_gib_per_dp,
        "wm_active_main_gib_per_dp": wm_active_main_gib_per_dp,
        "iter_count": len(kv_pcts),
        "kv_pct_peak": max(kv_pcts) if kv_pcts else None,
        "kv_pct_mean": (sum(kv_pcts) / len(kv_pcts)) if kv_pcts else None,
        "gen_req_peak": max(gen_reqs) if gen_reqs else None,
    }


def print_report(src: str, r: dict) -> None:
    dp = r["dp_size"]
    tp = r["tp_size"]
    bs = r["block_size"]
    wm = r["watermark"]
    plan = r["plan"]

    print(f"# GLM-5.2 Indexer K 与主 KV 容量匹配分析\n")
    print(f"- **日志来源**：`{src}`")
    print(f"- **D 节点拓扑**：`DP={dp}, TP={tp}`（`EP={dp * tp}`），`block_size={bs}`，`watermark={wm}`")
    if r["cfg_dram_gb"] is not None:
        print(f"- **本次配置 Sparse KV 池**：每 DP `{r['cfg_dram_gb']} GiB` × `{dp}` DP = 整机 **`{r['cfg_dram_gb'] * dp} GiB`**")
    print()

    print("## 1. 单 Token / 单 Block 体积与配比（GLM-5.2 实际分配方式）")
    print(f"- **主 KV（Host DRAM）**：`{r['main_layers']}` 层 × `(512 + 64)` × `2 B (BF16)` = **`{r['main_b_per_tok']:,} B/token`**（`{r['main_b_per_block']/1024**2:.4f} MiB/block`）")
    print(f"- **Indexer K（NPU HBM）**：`{r['indexer_layers']}` 层（GLM-5.2 `indexer_types` 中的 full 层，其余层共享）× `{r['indexer_b_per_layer_tok']:.2f} B/层/token` = **`{r['indexer_b_per_tok']:,.1f} B/token`**（`{r['indexer_b_per_block']/1024**2:.4f} MiB/block`，来源：{r['indexer_source']}）")
    print(f"- **主 KV : Indexer K 精确体积比**：**`{r['ratio']:.2f} : 1`**（每 `1 GiB` HBM Indexer K 跑满，需要 **`{r['ratio']:.2f} GiB`** 纯主 KV）")
    print()

    print("## 2. 本次日志中的 HBM Indexer K 与 Host DRAM 分配")
    if r["avail_hbm_gib"] is not None:
        print(f"- **单 die HBM 可用 KV 显存（`Available KV cache memory`）**：**`{r['avail_hbm_gib']:.2f} GiB`**（DP 组内 {tp} 颗 die 各存 1 份完整 Indexer K）")
    if plan:
        util_hbm = 100.0 * plan["final"] / plan["npu_limit"] if plan["npu_limit"] else 0.0
        print(f"- **NPU Indexer K 上限（`npu_limit`）**：**`{plan['npu_limit']:,}` blocks**（`{plan['npu_limit'] * bs:,}` tokens ≈ `{plan['npu_limit'] * bs / 1e6:.2f}M` tokens，占满 `{r['idx_cap_gib_per_dp']:.2f} GiB` HBM）")
        print(f"- **Host DRAM 主 KV 上限（`dram_limit`）**：**`{plan['dram_limit']:,}` blocks**（`{plan['dram_limit'] * bs:,}` tokens ≈ `{plan['dram_limit'] * bs / 1e6:.2f}M` tokens）")
        print(f"- **最终生效容量（`final`）**：**`{plan['final']:,}` blocks**（瓶颈：**`{plan['limiter']}`**）")
        print(f"  - 实际占用 Host DRAM 主 KV = **`{plan['host_gib']:.2f} GiB`**（+ 对齐预留 `{plan['align_gib']:.2f} GiB`）")
        print(f"  - 实际使用 HBM Indexer K = **`{plan['device_gib']:.2f} GiB`** / `{r['idx_cap_gib_per_dp']:.2f} GiB`（**HBM Indexer K 利用率仅 `{util_hbm:.1f}%`**）")
    else:
        print("- 日志中未匹配到 `Sparse KV offload memory plan` 行（可能未开启 `sparse_kv_offload` 或日志被截断）。")
    print()

    print("## 3. 把当前 HBM 里的 Indexer K 全部跑满，需要多少主 KV（Host DRAM）？")
    if r["full_main_pure_gib_per_dp"] is not None:
        va_limit = 4096 // tp
        pure_dp = r["full_main_pure_gib_per_dp"]
        cfg_dp = r["full_main_cfg_gib_per_dp"]
        wm_dp = r["wm_active_main_gib_per_dp"]
        print(f"| 口径 | 单 DP 组（TP{tp}） | 整机合计（DP{dp}） | 说明 |")
        print(f"|---|---:|---:|---|")
        print(f"| **HBM Indexer K 满载容量** | `{r['idx_cap_gib_per_dp']:.2f} GiB` | `{r['idx_cap_gib_per_dp'] * dp:.2f} GiB`（逻辑独立） | 每 DP 可容纳 `{r['npu_blocks']:,}` blocks（`{r['npu_blocks'] * bs / 1e6:.2f}M` tok） |")
        print(f"| **100% 跑满所需纯主 KV** | **`{pure_dp:.2f} GiB`** | **`{pure_dp * dp:.2f} GiB`** | `npu_limit × {r['main_b_per_block']/1024**2:.4f} MiB` |")
        print(f"| **建议配置 `dram_size_per_dp_GB`** | **`{cfg_dp} GiB`** | **`{cfg_dp * dp} GiB`** | 含约 0.3% 对齐预留；当前 4T 补丁单 DP 虚地址上限 `{va_limit} GiB` |")
        print(f"| **按 `watermark={wm}` 触发等待时的活跃主 KV** | `{wm_dp:.2f} GiB` | `{wm_dp * dp:.2f} GiB` | 对应逻辑 KV 使用率 `{100*(1-wm):.0f}%` |")
        print()
        if cfg_dp > va_limit:
            print(f"> **结论**：当前 `DP={dp}, TP={tp}` 下，跑满 HBM Indexer K 需要单 DP **`{cfg_dp} GiB`** 主 KV，超过 4T 补丁单 DP 上限 `{va_limit} GiB`（配到 `256 GiB` 只能用到约 `{100.0 * va_limit / cfg_dp:.1f}%` 的 HBM Indexer K）。")
        else:
            print(f"> **结论**：当前 `DP={dp}, TP={tp}` 下，单 DP 虚地址上限为 `{va_limit} GiB`，可直接将 `dram_size_per_dp_GB` 配到 **`{cfg_dp} GiB`**（整机 `{cfg_dp * dp} GiB`）以 100% 跑满 HBM Indexer K。")
    else:
        print("- 缺少 `Available KV cache memory` / `npu_limit`，无法推算满载需求。")
    print()

    if r["iter_count"] > 0 and r["final_blocks"]:
        print("## 4. 本次实际运行期间的 KV / Indexer K 占用统计（基于 `d.log` Iteration）")
        pk = r["kv_pct_peak"]
        mn = r["kv_pct_mean"]
        host_cap = plan["host_gib"] if plan else (r["final_blocks"] * r["main_b_per_block"] / 1024**3)
        dev_cap = plan["device_gib"] if plan else (r["final_blocks"] * r["indexer_b_per_block"] / 1024**3)
        print(f"- **采样步数**：`{r['iter_count']:,}` 次迭代，峰值并发解码请求数：**`{r['gen_req_peak']}`**")
        print(f"- **逻辑 KV 使用率**：峰值 **`{pk:.1f}%`**，均值 **`{mn:.1f}%`**")
        print(f"- **折算单 DP 主 KV（DRAM）实际占用**：峰值 **`{host_cap * pk / 100:.2f} GiB`**，均值 **`{host_cap * mn / 100:.2f} GiB`**")
        print(f"- **折算单 DP Indexer K（HBM）实际占用**：峰值 **`{dev_cap * pk / 100:.2f} GiB`**，均值 **`{dev_cap * mn / 100:.2f} GiB`**（占可用 HBM KV `{r['idx_cap_gib_per_dp']:.2f} GiB` 的峰值 **`{100 * (dev_cap * pk / 100) / r['idx_cap_gib_per_dp']:.1f}%`**）")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate GLM-5.2 Indexer K size and required Main KV DRAM from d.log")
    parser.add_argument("log", nargs="?", help="Path to local d.log (omit to auto-fetch latest from D node via cluster.env)")
    parser.add_argument("--run", help="Optional filter like F1/r6/perf when auto-fetching from D node")
    parser.add_argument("--config", type=Path, help="Path to GLM-5.2 config.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    env = load_cluster_env(repo)
    cfg_path = args.config
    if cfg_path is None and env.get("MODEL_DIR"):
        cand = Path(env["MODEL_DIR"]) / "config.json"
        if cand.is_file():
            cfg_path = cand
    model = parse_model_config(cfg_path)

    if args.log:
        p = Path(args.log)
        if p.is_dir():
            cand = p / "logs/d.log"
            p = cand if cand.is_file() else p
        text = p.read_text(encoding="utf-8", errors="ignore")
        src = str(p)
    else:
        src, text = read_remote_latest_dlog(repo, env, args.run)

    res = analyze_text(text, model)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print_report(src, res)


if __name__ == "__main__":
    main()
