#!/usr/bin/env python3
"""Comprehensive post-run metrics exporter for AgentX and W2 runs.

Extracts and cross-validates from client events/summary, Prometheus telemetry (metrics.jsonl),
and engine logs (d.log / d-kv-summary.log / p.log):
  - Throughput (output/input/total tok/s, req/s, completed/failed counts)
  - Latency breakdowns (TTFT mean/P50/P90/P95/P99/max, TPOT mean/P50/P90/P95/P99/max, E2E)
  - Actual batch sizes (client inflight BS, P running batch total & per-P & per-DP,
    D running batch total & per-DP from both metrics.jsonl and d.log Iteration lines)
  - P/D queueing (P waiting total & per-P, D total waiting & capacity waiting)
  - Prefix cache hit breakdown (total / local HBM / external Mooncake Store, overall & per-P)
  - Store occupancy (P Store & D Store mean/peak GiB & utilization %)
  - D Indexer K (HBM) and Main KV (Host DRAM) allocated capacity, actual mean/peak GiB,
    HBM vs DRAM bottleneck diagnosis, and per-DP load balance.

Usage:
  python3 tools/export_detailed_metrics.py runs/DP4_128/agentx_r1_d7200_c32/perf/p1
  python3 tools/export_detailed_metrics.py runs/F1/r6 --fetch-dlog
  python3 tools/export_detailed_metrics.py run1 run2 --compare --markdown
  python3 tools/export_detailed_metrics.py run1 run2 --csv > compare.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_indexer_kv as idx_mod


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def read_jsonl(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def percentile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int((len(s) - 1) * q))]


def stats_summary(vals: list[float]) -> dict:
    if not vals:
        return {"mean": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    return {
        "mean": statistics.fmean(vals),
        "p50": percentile(vals, 0.50),
        "p90": percentile(vals, 0.90),
        "p95": percentile(vals, 0.95),
        "p99": percentile(vals, 0.99),
        "max": max(vals),
    }


def time_stats(samples: list[dict], lo: float, hi: float, fn) -> dict:
    area = 0.0
    covered = 0.0
    vals = []
    for x, y in zip(samples, samples[1:]):
        dt = min(hi, y["time"]) - max(lo, x["time"])
        if dt > 0:
            v = fn(x)
            if v is not None:
                area += dt * v
                covered += dt
                vals.append(v)
    return {
        "mean": (area / covered) if covered > 0 else None,
        "p50": percentile(vals, 0.50),
        "p95": percentile(vals, 0.95),
        "max": max(vals) if vals else None,
    }


def sample_at_or_before(samples: list[dict], ts: float) -> dict:
    chosen = samples[0]
    for s in samples:
        if s["time"] <= ts:
            chosen = s
        else:
            break
    return chosen


def sample_at_or_after(samples: list[dict], ts: float) -> dict:
    for s in samples:
        if s["time"] >= ts:
            return s
    return samples[-1]


def resolve_perf_dir(p: Path) -> Path:
    if (p / "params.json").is_file():
        return p
    for cand in (p / "perf/p1", p / "p1"):
        if (cand / "params.json").is_file():
            return cand
    raise SystemExit(f"Cannot locate params.json under {p}")


def inflight_bs_from_events(events: list[dict], lo: float, hi: float) -> dict:
    changes: list[tuple[float, int]] = []
    for ev in events:
        t = ev.get("wall_time")
        e = ev.get("event")
        if t is None:
            continue
        if e == "send":
            changes.append((float(t), +1))
        elif e in ("done", "error", "cancelled"):
            changes.append((float(t), -1))
    if not changes:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    changes.sort(key=lambda x: x[0])
    cur = 0
    area = 0.0
    covered = 0.0
    peak = 0
    prev_t = changes[0][0]
    for t, delta in changes:
        seg_lo = max(lo, prev_t)
        seg_hi = min(hi, t)
        if seg_hi > seg_lo:
            area += (seg_hi - seg_lo) * cur
            covered += (seg_hi - seg_lo)
            if cur > peak:
                peak = cur
        cur = max(0, cur + delta)
        if lo <= t <= hi and cur > peak:
            peak = cur
        prev_t = t
    if hi > prev_t and cur > 0:
        seg_lo = max(lo, prev_t)
        area += (hi - seg_lo) * cur
        covered += (hi - seg_lo)
    return {
        "mean": (area / covered) if covered > 0 else 0.0,
        "max": float(peak),
    }


def parse_dlog_iterations(text: str) -> dict:
    # Example:
    # (APIServer pid=13) INFO 11-28 06:56:37 [loggers.py:185] Engine 000: Iteration(5982): 0 context requests, 0 context tokens, 25 generation requests, 25 generation tokens, iteration elapsed time: 113.78 ms, NPU KV cache usage: 62.7%
    pat = re.compile(
        r"Engine\s+(\d+):\s+Iteration\(\d+\):\s*"
        r"(\d+)\s+context requests,\s*(\d+)\s+context tokens,\s*"
        r"(\d+)\s+generation requests,\s*(\d+)\s+generation tokens,\s*"
        r"iteration elapsed time:\s*([0-9.]+)\s*ms,\s*"
        r"NPU KV cache usage:\s*([0-9.]+)%"
    )
    by_eng: dict[str, dict[str, list[float]]] = {}
    all_gen, all_ms, all_kv = [], [], []
    for m in pat.finditer(text):
        eng = f"DP{int(m.group(1))}"
        gen = float(m.group(4))
        ms = float(m.group(6))
        kv = float(m.group(7))
        all_gen.append(gen)
        all_ms.append(ms)
        all_kv.append(kv)
        b = by_eng.setdefault(eng, {"gen": [], "ms": [], "kv": []})
        b["gen"].append(gen)
        b["ms"].append(ms)
        b["kv"].append(kv)
    per_dp = {
        eng: {
            "iters": len(v["gen"]),
            "gen_bs_mean": statistics.fmean(v["gen"]),
            "gen_bs_p95": percentile(v["gen"], 0.95),
            "gen_bs_max": max(v["gen"]),
            "iter_ms_mean": statistics.fmean(v["ms"]),
            "iter_ms_p95": percentile(v["ms"], 0.95),
            "kv_pct_mean": statistics.fmean(v["kv"]),
            "kv_pct_max": max(v["kv"]),
        }
        for eng, v in sorted(by_eng.items())
    }
    return {
        "total_iters": len(all_gen),
        "engine_count": len(by_eng),
        "single_dp_gen_bs": stats_summary(all_gen),
        "iter_ms": stats_summary(all_ms),
        "kv_pct": stats_summary(all_kv),
        "per_dp": per_dp,
    }


def parse_plog_iterations(text: str) -> dict:
    pat = re.compile(
        r"Engine\s+(\d+):\s+Iteration\(\d+\):\s*"
        r"(\d+)\s+context requests,\s*(\d+)\s+context tokens,\s*"
        r"(\d+)\s+generation requests,\s*(\d+)\s+generation tokens,\s*"
        r"iteration elapsed time:\s*([0-9.]+)\s*ms,\s*"
        r"NPU KV cache usage:\s*([0-9.]+)%"
    )
    ctx_reqs, ctx_toks, iter_ms, kv_pct = [], [], [], []
    for m in pat.finditer(text):
        ctx_reqs.append(float(m.group(2)))
        ctx_toks.append(float(m.group(3)))
        iter_ms.append(float(m.group(6)))
        kv_pct.append(float(m.group(7)))
    return {
        "total_iters": len(ctx_reqs),
        "ctx_bs": stats_summary(ctx_reqs),
        "ctx_tokens": stats_summary(ctx_toks),
        "iter_ms": stats_summary(iter_ms),
        "kv_pct": stats_summary(kv_pct),
    }


def extract_run(
    run_arg: Path,
    scope: str = "total",
    dlog_path: Path | None = None,
    fetch_dlog: bool = False,
    config_path: Path | None = None,
) -> dict:
    pdir = resolve_perf_dir(run_arg)
    g = read_json(pdir / "params.json")
    before = read_json(pdir / "before.json")
    after = read_json(pdir / "after.json")
    samples = [before] + read_jsonl(pdir / "metrics.jsonl") + [after]

    # Detect AgentX vs W2
    if (pdir / "agentx-client/summary.json").is_file():
        kind = "agentx"
        cdir = pdir / "agentx-client"
        csum = read_json(cdir / "summary.json")
        events = read_jsonl(cdir / "events.jsonl") if (cdir / "events.jsonl").is_file() else []
        if scope == "window":
            lo = csum["first_send_unix"]
            hi = min(csum["deadline_unix"], csum["last_done_unix"])
            done_recs = [e for e in events if e.get("event") == "done" and e.get("end_unix", 0) <= csum["deadline_unix"]]
            bucket = csum["in_window"]
        else:
            lo = csum["first_send_unix"]
            hi = csum["last_done_unix"]
            done_recs = [e for e in events if e.get("event") == "done"]
            bucket = csum["total_including_drain"]
        window_s = max(1e-6, hi - lo)
        req_completed = bucket["requests_completed"]
        req_failed = csum["requests_failed"]
        in_tokens = bucket["input_tokens"]
        out_tokens = bucket["output_tokens"]
        configured_c = csum["concurrency"]
        configured_dur_s = csum["configured_duration_s"]
        peak_inflight_reported = csum.get("peak_inflight_http")
    elif (pdir / "finite-client/summary.json").is_file():
        kind = "w2"
        cdir = pdir / "finite-client"
        csum = read_json(cdir / "summary.json")
        events = read_jsonl(cdir / "events.jsonl") if (cdir / "events.jsonl").is_file() else []
        lo = csum["first_send_unix"]
        hi = csum["last_done_unix"]
        done_recs = [e for e in events if e.get("event") == "done"]
        window_s = max(1e-6, hi - lo)
        req_completed = csum["requests"]
        req_failed = csum["failed"]
        in_tokens = csum["input_tokens"]
        out_tokens = csum["output_tokens"]
        configured_c = csum["concurrency"]
        configured_dur_s = csum["duration_s"]
        peak_inflight_reported = csum.get("peak_inflight_http")
    else:
        raise SystemExit(f"Neither agentx-client/summary.json nor finite-client/summary.json found in {pdir}")

    ttfts = [r["ttft_s"] for r in done_recs if r.get("ttft_s") is not None]
    tpots = [r["tpot_ms"] for r in done_recs if r.get("tpot_ms") is not None]
    e2es = [r["latency_s"] for r in done_recs if r.get("latency_s") is not None]
    ttft_st = stats_summary(ttfts)
    tpot_st = stats_summary(tpots)
    e2e_st = stats_summary(e2es)
    # Fallback to summary.json if events.jsonl was absent
    if ttft_st["mean"] is None:
        src_b = csum["in_window" if (kind == "agentx" and scope == "window") else "total_including_drain"] if kind == "agentx" else csum
        ttft_st["mean"] = src_b.get("ttft_mean_s")
        ttft_st["p50"] = src_b.get("ttft_p50_s")
        ttft_st["p95"] = src_b.get("ttft_p95_s")
        ttft_st["p99"] = src_b.get("ttft_p99_s")
        tpot_st["mean"] = src_b.get("tpot_mean_ms")
        tpot_st["p50"] = src_b.get("tpot_p50_ms")
        tpot_st["p95"] = src_b.get("tpot_p95_ms")

    inflight = inflight_bs_from_events(events, lo, hi)
    if inflight["max"] is None and peak_inflight_reported is not None:
        inflight["max"] = float(peak_inflight_reported)

    # Telemetry window slicing
    if kind == "agentx" and scope == "window":
        s_lo = sample_at_or_before(samples, lo)
        s_hi = sample_at_or_after(samples, hi)
    else:
        s_lo = samples[0]
        s_hi = samples[-1]

    ps = [p for p in ("P1", "P2", "P3") if p in before]
    p_count = g.get("p_count", len(ps))
    p_dp = g.get("p_dp", 1)
    d_dp = g.get("d_dp", 1)

    has_p_running = all("running" in s[ps[0]] for s in samples) if ps else False
    has_d_running = all("running" in s["D"] for s in samples)
    has_d_waiting = all("waiting" in s["D"] for s in samples)
    has_d_dp_metrics = all(isinstance(s["D"].get("dp_running"), list) and len(s["D"]["dp_running"]) > 0 for s in samples)

    per_p = {}
    q_tot = loc_tot = ext_tot = 0.0
    for p in ps:
        q = max(0.0, s_hi[p]["query"] - s_lo[p]["query"])
        lh = max(0.0, s_hi[p]["local_hit"] - s_lo[p]["local_hit"])
        eh = max(0.0, s_hi[p]["ext_hit"] - s_lo[p]["ext_hit"])
        q_tot += q
        loc_tot += lh
        ext_tot += eh
        per_p[p] = {
            "query_tokens": q,
            "local_hit_tokens": lh,
            "ext_hit_tokens": eh,
            "total_hit_pct": (100.0 * (lh + eh) / q) if q > 0 else 0.0,
            "local_hit_pct": (100.0 * lh / q) if q > 0 else 0.0,
            "ext_hit_pct": (100.0 * eh / q) if q > 0 else 0.0,
            "waiting": time_stats(samples, lo, hi, lambda x, p=p: x[p]["waiting"]),
            "running": time_stats(samples, lo, hi, lambda x, p=p: x[p].get("running")) if has_p_running else None,
            "kv_usage_pct": time_stats(samples, lo, hi, lambda x, p=p: 100.0 * x[p]["kv_usage"] if "kv_usage" in x[p] else None),
            "store_gib": time_stats(samples, lo, hi, lambda x, p=p: x["master"][p + "_segment_allocated_bytes"]),
        }

    p_wait_tot = time_stats(samples, lo, hi, lambda x: sum(x[p]["waiting"] for p in ps))
    p_run_tot = time_stats(samples, lo, hi, lambda x: sum(x[p]["running"] for p in ps)) if has_p_running else None
    p_store_tot = time_stats(samples, lo, hi, lambda x: sum(x["master"][p + "_segment_allocated_bytes"] for p in ps))
    d_store_tot = time_stats(samples, lo, hi, lambda x: x["master"].get("D_segment_allocated_bytes", 0.0))
    d_cap_wait = time_stats(samples, lo, hi, lambda x: x["D"]["capacity"])
    d_wait_tot = time_stats(samples, lo, hi, lambda x: x["D"].get("waiting")) if has_d_waiting else None
    d_run_tot = time_stats(samples, lo, hi, lambda x: x["D"].get("running")) if has_d_running else None
    d_kv_pct = time_stats(samples, lo, hi, lambda x: 100.0 * x["D"]["kv_usage"])

    d_dp_telemetry = {}
    if has_d_dp_metrics:
        n_dp_m = len(samples[0]["D"]["dp_running"])
        for idx in range(n_dp_m):
            d_dp_telemetry[f"DP{idx}"] = {
                "running": time_stats(samples, lo, hi, lambda x, i=idx: x["D"]["dp_running"][i] if i < len(x["D"].get("dp_running", [])) else None),
                "waiting": time_stats(samples, lo, hi, lambda x, i=idx: x["D"]["dp_waiting"][i] if i < len(x["D"].get("dp_waiting", [])) else None),
                "kv_usage_pct": time_stats(samples, lo, hi, lambda x, i=idx: 100.0 * x["D"]["dp_kv_usage"][i] if i < len(x["D"].get("dp_kv_usage", [])) else None),
            }

    # Resolve d.log / d-kv-summary.log
    repo = Path(__file__).resolve().parent.parent
    env = idx_mod.load_cluster_env(repo)
    if config_path is None and env.get("MODEL_DIR"):
        cand = Path(env["MODEL_DIR"]) / "config.json"
        if cand.is_file():
            config_path = cand
    model_cfg = idx_mod.parse_model_config(config_path)

    dlog_text = None
    dlog_src = None
    for cand in ([dlog_path] if dlog_path else []) + [
        pdir / "d-kv-summary.log",
        pdir.parent / "d/logs/d.log",
    ]:
        if cand and cand.is_file() and cand.stat().st_size > 0:
            dlog_text = cand.read_text(encoding="utf-8", errors="ignore")
            dlog_src = str(cand)
            break
    if dlog_text is None and fetch_dlog:
        parts = pdir.parts
        run_filter = "/".join(parts[-4:-1]) if len(parts) >= 4 else None
        dlog_src, dlog_text = idx_mod.read_remote_latest_dlog(repo, env, run_filter)

    d_kv_analysis = idx_mod.analyze_text(dlog_text, model_cfg) if dlog_text else None
    d_log_iters = parse_dlog_iterations(dlog_text) if dlog_text else None

    # Optional P1 local p.log iteration summary
    p1_log = pdir / "logs/p.log"
    p1_log_iters = parse_plog_iterations(p1_log.read_text(encoding="utf-8", errors="ignore")) if p1_log.is_file() else None

    # Compute exact DRAM Main KV & HBM Indexer K actual occupancy (mean & peak GiB)
    # Use exact memory plan from d.log when available; otherwise compute analytically from params.json + GLM-5.2 constants!
    main_b_per_block = 78 * (512 + 64) * 2 * 128  # 11,501,568 B
    idx_b_per_block = 21 * 256 * 128              # 688,128 B
    if d_kv_analysis and d_kv_analysis["plan"]:
        pl = d_kv_analysis["plan"]
        per_dp_dram_alloc_gib = pl["host_gib"]
        per_dp_idx_alloc_gib = pl["device_gib"]
        avail_hbm_gib = d_kv_analysis["avail_hbm_gib"]
        limiter = pl["limiter"]
        full_main_cfg_dp = d_kv_analysis["full_main_cfg_gib_per_dp"]
        hbm_util_alloc_pct = 100.0 * pl["final"] / pl["npu_limit"] if pl["npu_limit"] else None
        kv_source = f"d.log 实测 ({dlog_src})"
    else:
        # Analytical fallback from params.json (exact 16.7143:1 ratio verified on GLM-5.2)
        per_dp_dram_alloc_gib = g["sparse"] * 0.9974
        per_dp_idx_alloc_gib = per_dp_dram_alloc_gib / (main_b_per_block / idx_b_per_block)
        avail_hbm_gib = d_kv_analysis["avail_hbm_gib"] if d_kv_analysis else None
        limiter = "未读取 d.log（按理论比例折算）"
        full_main_cfg_dp = d_kv_analysis["full_main_cfg_gib_per_dp"] if d_kv_analysis else None
        hbm_util_alloc_pct = (100.0 * per_dp_idx_alloc_gib / avail_hbm_gib) if avail_hbm_gib else None
        kv_source = "按 params.json 与 GLM-5.2 实测比值 16.71:1 折算"

    kv_mean_frac = (d_kv_pct["mean"] / 100.0) if d_kv_pct["mean"] is not None else 0.0
    kv_peak_frac = (d_kv_pct["max"] / 100.0) if d_kv_pct["max"] is not None else 0.0
    dram_mean_per_dp = per_dp_dram_alloc_gib * kv_mean_frac
    dram_peak_per_dp = per_dp_dram_alloc_gib * kv_peak_frac
    idx_mean_per_dp = per_dp_idx_alloc_gib * kv_mean_frac
    idx_peak_per_dp = per_dp_idx_alloc_gib * kv_peak_frac

    # Best available D running batch (prefer telemetry metrics.jsonl, fallback to d.log iterations)
    if d_run_tot and d_run_tot["mean"] is not None:
        d_bs_mean = d_run_tot["mean"]
        d_bs_p95 = d_run_tot["p95"]
        d_bs_max = d_run_tot["max"]
        d_bs_source = "metrics.jsonl"
    elif d_log_iters and d_log_iters["total_iters"] > 0:
        eng_cnt = max(1, d_log_iters["engine_count"])
        d_bs_mean = d_log_iters["single_dp_gen_bs"]["mean"] * eng_cnt
        d_bs_p95 = (d_log_iters["single_dp_gen_bs"]["p95"] or 0.0) * eng_cnt
        d_bs_max = (d_log_iters["single_dp_gen_bs"]["max"] or 0.0) * eng_cnt
        d_bs_source = f"d.log ({eng_cnt} DP 合计估算)"
    else:
        d_bs_mean = d_bs_p95 = d_bs_max = None
        d_bs_source = "N/A"

    p_bs_mean = p_run_tot["mean"] if (p_run_tot and p_run_tot["mean"] is not None) else (
        p1_log_iters["ctx_bs"]["mean"] if (p1_log_iters and p1_log_iters["total_iters"] > 0) else None
    )
    p_bs_max = p_run_tot["max"] if (p_run_tot and p_run_tot["max"] is not None) else (
        p1_log_iters["ctx_bs"]["max"] if (p1_log_iters and p1_log_iters["total_iters"] > 0) else None
    )
    p_bs_source = "metrics.jsonl" if (p_run_tot and p_run_tot["mean"] is not None) else ("p1 p.log" if p1_log_iters and p1_log_iters["total_iters"] > 0 else "N/A")

    p_store_cap = g["ps"] * p_count
    d_store_cap = g["ds"]
    d_sparse_total_cap = g["sparse"] * d_dp

    return {
        "run_path": str(pdir),
        "kind": kind,
        "scope": scope,
        "case": g["case"],
        "topology": f"{p_count}P(DP{p_dp}xTP{16//p_dp})+1D(DP{d_dp}xTP{16//d_dp})",
        "p_count": p_count,
        "p_dp": p_dp,
        "d_dp": d_dp,
        "quota": {
            "p_store_per_node_gib": g["ps"],
            "p_store_total_gib": p_store_cap,
            "d_store_gib": d_store_cap,
            "d_sparse_per_dp_gib": g["sparse"],
            "d_sparse_total_gib": d_sparse_total_cap,
            "total_budget_gib": p_store_cap + d_store_cap + d_sparse_total_cap,
        },
        "throughput": {
            "concurrency_roots": configured_c,
            "configured_duration_s": configured_dur_s,
            "actual_window_s": window_s,
            "requests_completed": req_completed,
            "requests_failed": req_failed,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "avg_input_tokens": (in_tokens / req_completed) if req_completed else 0.0,
            "avg_output_tokens": (out_tokens / req_completed) if req_completed else 0.0,
            "output_tok_s": out_tokens / window_s,
            "input_tok_s": in_tokens / window_s,
            "total_tok_s": (in_tokens + out_tokens) / window_s,
            "req_s": req_completed / window_s,
        },
        "latency": {
            "ttft_s": ttft_st,
            "tpot_ms": tpot_st,
            "e2e_s": e2e_st,
        },
        "batch_and_queue": {
            "client_inflight_bs_mean": inflight["mean"],
            "client_inflight_bs_max": inflight["max"],
            "p_running_bs_mean": p_bs_mean,
            "p_running_bs_max": p_bs_max,
            "p_running_bs_source": p_bs_source,
            "d_running_bs_mean": d_bs_mean,
            "d_running_bs_p95": d_bs_p95,
            "d_running_bs_max": d_bs_max,
            "d_running_bs_per_dp_mean": (d_bs_mean / d_dp) if d_bs_mean is not None else None,
            "d_running_bs_source": d_bs_source,
            "p_waiting_mean": p_wait_tot["mean"],
            "p_waiting_max": p_wait_tot["max"],
            "d_waiting_mean": d_wait_tot["mean"] if d_wait_tot else None,
            "d_waiting_max": d_wait_tot["max"] if d_wait_tot else None,
            "d_capacity_wait_mean": d_cap_wait["mean"],
            "d_capacity_wait_max": d_cap_wait["max"],
        },
        "prefix_cache": {
            "query_tokens": q_tot,
            "local_hit_tokens": loc_tot,
            "ext_hit_tokens": ext_tot,
            "total_hit_pct": (100.0 * (loc_tot + ext_tot) / q_tot) if q_tot > 0 else 0.0,
            "local_hit_pct": (100.0 * loc_tot / q_tot) if q_tot > 0 else 0.0,
            "ext_hit_pct": (100.0 * ext_tot / q_tot) if q_tot > 0 else 0.0,
        },
        "store": {
            "p_store_mean_gib": p_store_tot["mean"],
            "p_store_max_gib": p_store_tot["max"],
            "p_store_util_mean_pct": (100.0 * p_store_tot["mean"] / p_store_cap) if (p_store_cap and p_store_tot["mean"] is not None) else 0.0,
            "d_store_mean_gib": d_store_tot["mean"],
            "d_store_max_gib": d_store_tot["max"],
        },
        "d_kv_and_indexer": {
            "source": kv_source,
            "limiter": limiter,
            "d_kv_usage_mean_pct": d_kv_pct["mean"],
            "d_kv_usage_p95_pct": d_kv_pct["p95"],
            "d_kv_usage_max_pct": d_kv_pct["max"],
            "dram_alloc_per_dp_gib": per_dp_dram_alloc_gib,
            "dram_alloc_total_gib": per_dp_dram_alloc_gib * d_dp,
            "dram_used_mean_per_dp_gib": dram_mean_per_dp,
            "dram_used_peak_per_dp_gib": dram_peak_per_dp,
            "dram_used_mean_total_gib": dram_mean_per_dp * d_dp,
            "dram_used_peak_total_gib": dram_peak_per_dp * d_dp,
            "hbm_avail_per_die_gib": avail_hbm_gib,
            "indexer_alloc_per_dp_gib": per_dp_idx_alloc_gib,
            "indexer_alloc_total_logical_gib": per_dp_idx_alloc_gib * d_dp,
            "indexer_hbm_alloc_util_pct": hbm_util_alloc_pct,
            "indexer_used_mean_per_dp_gib": idx_mean_per_dp,
            "indexer_used_peak_per_dp_gib": idx_peak_per_dp,
            "indexer_used_mean_total_logical_gib": idx_mean_per_dp * d_dp,
            "indexer_used_peak_total_logical_gib": idx_peak_per_dp * d_dp,
            "dram_needed_to_fill_hbm_per_dp_gib": full_main_cfg_dp,
        },
        "per_p": per_p,
        "d_dp_telemetry": d_dp_telemetry,
        "d_log_iterations": d_log_iters,
        "p1_log_iterations": p1_log_iters,
    }


def fmt(v, spec=".2f", suffix=""):
    if v is None:
        return "-"
    return f"{v:{spec}}{suffix}"


def flatten_row(r: dict) -> dict:
    q = r["quota"]
    t = r["throughput"]
    l = r["latency"]
    b = r["batch_and_queue"]
    pc = r["prefix_cache"]
    st = r["store"]
    kv = r["d_kv_and_indexer"]
    return {
        "组别": r["case"],
        "拓扑": r["topology"],
        "配额(PSt/DSt/Sp)": f"{q['p_store_total_gib']}/{q['d_store_gib']}/{q['d_sparse_per_dp_gib']}x{r['d_dp']}",
        "总预算GiB": q["total_budget_gib"],
        "并发c": t["concurrency_roots"],
        "窗口s": round(t["actual_window_s"], 1),
        "完成请求": t["requests_completed"],
        "失败": t["requests_failed"],
        "输出tok/s": round(t["output_tok_s"], 2),
        "输入tok/s": round(t["input_tok_s"], 1),
        "TTFT均值s": round(l["ttft_s"]["mean"], 3) if l["ttft_s"]["mean"] is not None else None,
        "TTFT_P50s": round(l["ttft_s"]["p50"], 3) if l["ttft_s"]["p50"] is not None else None,
        "TTFT_P95s": round(l["ttft_s"]["p95"], 3) if l["ttft_s"]["p95"] is not None else None,
        "TPOT均值ms": round(l["tpot_ms"]["mean"], 2) if l["tpot_ms"]["mean"] is not None else None,
        "TPOT_P50ms": round(l["tpot_ms"]["p50"], 2) if l["tpot_ms"]["p50"] is not None else None,
        "TPOT_P95ms": round(l["tpot_ms"]["p95"], 2) if l["tpot_ms"]["p95"] is not None else None,
        "在途BS均/峰": f"{fmt(b['client_inflight_bs_mean'], '.1f')}/{fmt(b['client_inflight_bs_max'], '.0f')}",
        "P_Running均/峰": f"{fmt(b['p_running_bs_mean'], '.1f')}/{fmt(b['p_running_bs_max'], '.0f')}",
        "D_Running均/峰": f"{fmt(b['d_running_bs_mean'], '.1f')}/{fmt(b['d_running_bs_max'], '.0f')}",
        "P排队均值": round(b["p_waiting_mean"], 2) if b["p_waiting_mean"] is not None else None,
        "D容量等待": round(b["d_capacity_wait_mean"], 2) if b["d_capacity_wait_mean"] is not None else None,
        "P总命中率%": round(pc["total_hit_pct"], 2),
        "P本地命中%": round(pc["local_hit_pct"], 2),
        "PStore命中%": round(pc["ext_hit_pct"], 2),
        "PStore均/峰GiB": f"{fmt(st['p_store_mean_gib'], '.1f')}/{fmt(st['p_store_max_gib'], '.1f')}",
        "DKV使用率均/峰%": f"{fmt(kv['d_kv_usage_mean_pct'], '.1f')}/{fmt(kv['d_kv_usage_max_pct'], '.1f')}",
        "DRAM主KV均/峰GiB": f"{fmt(kv['dram_used_mean_total_gib'], '.1f')}/{fmt(kv['dram_used_peak_total_gib'], '.1f')}",
        "IndexK_HBM单DP均/峰GiB": f"{fmt(kv['indexer_used_mean_per_dp_gib'], '.2f')}/{fmt(kv['indexer_used_peak_per_dp_gib'], '.2f')}",
        "IndexK分配/可用HBM": f"{fmt(kv['indexer_alloc_per_dp_gib'], '.2f')}/{fmt(kv['hbm_avail_per_die_gib'], '.2f')}",
        "瓶颈": kv["limiter"],
    }


def render_single_markdown(r: dict) -> str:
    q = r["quota"]
    t = r["throughput"]
    l = r["latency"]
    b = r["batch_and_queue"]
    pc = r["prefix_cache"]
    st = r["store"]
    kv = r["d_kv_and_indexer"]

    out = [
        f"# 详细性能与双池占用报告：{r['case']}（{r['topology']}）",
        "",
        f"- **目录**：`{r['run_path']}` | **Workload**：`{r['kind']}` | **口径**：`{r['scope']}`",
        f"- **配额**：P Store `{q['p_store_per_node_gib']} GiB × {r['p_count']} = {q['p_store_total_gib']} GiB` | D Store `{q['d_store_gib']} GiB` | D Sparse `{q['d_sparse_per_dp_gib']} GiB × {r['d_dp']} DP = {q['d_sparse_total_gib']} GiB` | **总预算 `{q['total_budget_gib']} GiB`**",
        "",
        "## 1. 吞吐（Throughput）与延迟分位（TTFT / TPOT / E2E）",
        "| 指标 | 数值 | 指标 | 数值 |",
        "|---|---:|---|---:|",
        f"| **输出吞吐（Output Thpt）** | **{fmt(t['output_tok_s'], '.2f', ' tok/s')}** | **输入吞吐（Input Thpt）** | {fmt(t['input_tok_s'], '.1f', ' tok/s')} |",
        f"| **总吞吐（Total Thpt）** | {fmt(t['total_tok_s'], '.1f', ' tok/s')} | **请求吞吐** | {fmt(t['req_s'], '.3f', ' req/s')} |",
        f"| **完成 / 失败请求** | `{t['requests_completed']:,}` / `{t['requests_failed']}` | **统计窗口 / 并发根树** | `{t['actual_window_s']:.1f} s` / `c={t['concurrency_roots']}` |",
        f"| **平均输入 / 输出长度** | `{t['avg_input_tokens']:,.0f}` / `{t['avg_output_tokens']:,.0f}` tok | **总输入 / 输出 tokens** | `{t['input_tokens']:,}` / `{t['output_tokens']:,}` |",
        f"| **TTFT 均值 / P50** | **{fmt(l['ttft_s']['mean'], '.2f', ' s')}** / {fmt(l['ttft_s']['p50'], '.2f', ' s')} | **TTFT P90 / P95 / P99 / Max** | {fmt(l['ttft_s']['p90'], '.2f')} / **{fmt(l['ttft_s']['p95'], '.2f')}** / {fmt(l['ttft_s']['p99'], '.2f')} / {fmt(l['ttft_s']['max'], '.2f', ' s')} |",
        f"| **TPOT 均值 / P50** | **{fmt(l['tpot_ms']['mean'], '.2f', ' ms')}** / {fmt(l['tpot_ms']['p50'], '.2f', ' ms')} | **TPOT P90 / P95 / P99 / Max** | {fmt(l['tpot_ms']['p90'], '.2f')} / **{fmt(l['tpot_ms']['p95'], '.2f')}** / {fmt(l['tpot_ms']['p99'], '.2f')} / {fmt(l['tpot_ms']['max'], '.2f', ' ms')} |",
        f"| **E2E 均值 / P50 / P95** | {fmt(l['e2e_s']['mean'], '.2f')} / {fmt(l['e2e_s']['p50'], '.2f')} / {fmt(l['e2e_s']['p95'], '.2f', ' s')} | **客户端实际在途 BS（均/峰）** | **{fmt(b['client_inflight_bs_mean'], '.2f')}** / **{fmt(b['client_inflight_bs_max'], '.0f')}** |",
        "",
        "## 2. P / D 实际 Running Batch、排队与前缀命中率",
        "| 维度 | 均值 | P95 | 峰值 | 来源 / 说明 |",
        "|---|---:|---:|---:|---|",
        f"| **P Running Batch（合计）** | **{fmt(b['p_running_bs_mean'], '.2f')}** | - | **{fmt(b['p_running_bs_max'], '.0f')}** | `{b['p_running_bs_source']}` |",
        f"| **D Running Batch（整机合计 / 单DP均）** | **{fmt(b['d_running_bs_mean'], '.2f')}** ({fmt(b['d_running_bs_per_dp_mean'], '.2f')}/DP) | {fmt(b['d_running_bs_p95'], '.1f')} | **{fmt(b['d_running_bs_max'], '.0f')}** | `{b['d_running_bs_source']}` |",
        f"| **P 平均排队数（合计）** | **{fmt(b['p_waiting_mean'], '.2f')}** | - | {fmt(b['p_waiting_max'], '.0f')} | 全部活跃 P 节点之和 |",
        f"| **D 总排队数 / 容量等待数** | {fmt(b['d_waiting_mean'], '.2f')} / **{fmt(b['d_capacity_wait_mean'], '.2f')}** | - | {fmt(b['d_waiting_max'], '.0f')} / {fmt(b['d_capacity_wait_max'], '.0f')} | `reason=capacity` 为 KV 水位限流等待 |",
        f"| **P 合计前缀命中率（总 = 本地 + Store）** | **{pc['total_hit_pct']:.2f}%** | - | - | 本地 HBM `{pc['local_hit_pct']:.2f}%` + Mooncake Store `{pc['ext_hit_pct']:.2f}%` |",
        f"| **P Store 对象占用（合计）** | **{fmt(st['p_store_mean_gib'], '.1f', ' GiB')}** ({st['p_store_util_mean_pct']:.1f}%) | - | {fmt(st['p_store_max_gib'], '.1f', ' GiB')} | 总注册 `{q['p_store_total_gib']} GiB` |",
        f"| **D Store 对象占用** | {fmt(st['d_store_mean_gib'], '.1f', ' GiB')} | - | {fmt(st['d_store_max_gib'], '.1f', ' GiB')} | 配额 `{q['d_store_gib']} GiB` |",
        "",
        "## 3. D 节点 Indexer K（HBM）与主 KV（DRAM）精确占用",
        f"- **数据来源**：`{kv['source']}` | **容量瓶颈判定**：**`{kv['limiter']}`**",
        "| 存储层级 | 分配容量（单DP / 整机） | 运行期均值（单DP / 整机） | 运行期峰值（单DP / 整机） | 备注 |",
        "|---|---:|---:|---:|---|",
        f"| **D 逻辑 KV 使用率** | 100% | **{fmt(kv['d_kv_usage_mean_pct'], '.1f', '%')}** | **{fmt(kv['d_kv_usage_max_pct'], '.1f', '%')}** (P95 {fmt(kv['d_kv_usage_p95_pct'], '.1f', '%')}) | watermark 线 80% |",
        f"| **主 KV（Host DRAM）** | `{kv['dram_alloc_per_dp_gib']:.2f}` / **`{kv['dram_alloc_total_gib']:.2f} GiB`** | **`{kv['dram_used_mean_per_dp_gib']:.2f}` / `{kv['dram_used_mean_total_gib']:.2f} GiB`** | **`{kv['dram_used_peak_per_dp_gib']:.2f}` / `{kv['dram_used_peak_total_gib']:.2f} GiB`** | 78 层 BF16 MLA (`89,856 B/tok`) |",
        f"| **Indexer K（NPU HBM）** | `{kv['indexer_alloc_per_dp_gib']:.2f}` / `{kv['indexer_alloc_total_logical_gib']:.2f} GiB` | **`{kv['indexer_used_mean_per_dp_gib']:.2f}` / `{kv['indexer_used_mean_total_logical_gib']:.2f} GiB`** | **`{kv['indexer_used_peak_per_dp_gib']:.2f}` / `{kv['indexer_used_peak_total_logical_gib']:.2f} GiB`** | 单 die 可用 HBM `{fmt(kv['hbm_avail_per_die_gib'], '.2f', ' GiB')}`，分配利用率 `{fmt(kv['indexer_hbm_alloc_util_pct'], '.1f', '%')}`，跑满 HBM 需 `{fmt(kv['dram_needed_to_fill_hbm_per_dp_gib'], '.0f', ' GiB/DP')}` DRAM |",
        "",
        "## 4. 分 P 节点详细对比",
        "| 节点 | 查询 tokens | 总命中率 | 本地 HBM 命中 | Store 命中 | Running BS（均/峰） | 排队（均/峰） | HBM KV% 均值 | Store 占用（均/峰 GiB） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p, v in r["per_p"].items():
        r_str = f"{fmt(v['running']['mean'], '.2f')} / {fmt(v['running']['max'], '.0f')}" if v["running"] else "-"
        out.append(
            f"| {p} | {v['query_tokens']:,.0f} | **{v['total_hit_pct']:.2f}%** | {v['local_hit_pct']:.2f}% | {v['ext_hit_pct']:.2f}% | "
            f"{r_str} | {fmt(v['waiting']['mean'], '.2f')} / {fmt(v['waiting']['max'], '.0f')} | "
            f"{fmt(v['kv_usage_pct']['mean'], '.1f', '%')} | {fmt(v['store_gib']['mean'], '.1f')} / {fmt(v['store_gib']['max'], '.1f')} |"
        )

    if r["d_dp_telemetry"]:
        out += [
            "",
            "## 5. D 节点各 DP 组负载均衡（来自 `metrics.jsonl`）",
            "| DP 组 | Running BS 均值 | Running BS P95 | Running BS 峰值 | 等待均值 | 逻辑 KV% 均值 | 逻辑 KV% 峰值 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for dp_name, v in r["d_dp_telemetry"].items():
            out.append(
                f"| {dp_name} | {fmt(v['running']['mean'], '.2f')} | {fmt(v['running']['p95'], '.1f')} | {fmt(v['running']['max'], '.0f')} | "
                f"{fmt(v['waiting']['mean'], '.2f')} | {fmt(v['kv_usage_pct']['mean'], '.1f', '%')} | {fmt(v['kv_usage_pct']['max'], '.1f', '%')} |"
            )

    if r["d_log_iterations"] and r["d_log_iterations"]["total_iters"] > 0:
        dl = r["d_log_iterations"]
        out += [
            "",
            f"## 6. D 节点每步解码实际 BS 与步耗时（来自 `d.log`，共 `{dl['total_iters']:,}` 步）",
            "| Engine | 迭代步数 | Decode BS 均值 | Decode BS P95 | Decode BS 峰值 | 步耗时均值 (ms) | 步耗时 P95 (ms) | KV% 均值 / 峰值 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for eng, v in dl["per_dp"].items():
            out.append(
                f"| {eng} | {v['iters']:,} | {v['gen_bs_mean']:.2f} | {fmt(v['gen_bs_p95'], '.1f')} | {v['gen_bs_max']:.0f} | "
                f"{v['iter_ms_mean']:.2f} | {fmt(v['iter_ms_p95'], '.2f')} | {v['kv_pct_mean']:.1f}% / {v['kv_pct_max']:.1f}% |"
            )

    return "\n".join(out) + "\n"


def render_compare_markdown(rows: list[dict]) -> str:
    flat = [flatten_row(r) for r in rows]
    headers = list(flat[0].keys())
    lines = [
        "# 多组实验全指标对比表",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * 3 + ["---:"] * (len(headers) - 3)) + " |",
    ]
    for f in flat:
        lines.append("| " + " | ".join("-" if f[h] is None else str(f[h]) for h in headers) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export detailed throughput, latency, BS, queue, hit-rate, Indexer K & DRAM KV metrics")
    parser.add_argument("runs", type=Path, nargs="+", help="One or more run directories (e.g. runs/DP4_128/agentx_r1_d7200_c32/perf/p1)")
    parser.add_argument("--scope", choices=["total", "window"], default="total", help="AgentX metric scope (total includes drain; window is strict duration)")
    parser.add_argument("--dlog", type=Path, help="Explicit d.log or d-kv-summary.log path (single run)")
    parser.add_argument("--fetch-dlog", action="store_true", help="Fetch d.log summary remotely from D via cluster.env if missing locally")
    parser.add_argument("--config", type=Path, help="Optional GLM-5.2 config.json path")
    parser.add_argument("--compare", action="store_true", help="Render compact side-by-side comparison Markdown table")
    parser.add_argument("--markdown", action="store_true", help="Render Markdown report (default)")
    parser.add_argument("--csv", action="store_true", help="Render flat CSV table")
    parser.add_argument("--json", action="store_true", help="Render full nested JSON")
    args = parser.parse_args()

    if args.dlog and len(args.runs) != 1:
        parser.error("--dlog only supports a single run")

    results = [
        extract_run(p, scope=args.scope, dlog_path=args.dlog, fetch_dlog=args.fetch_dlog, config_path=args.config)
        for p in args.runs
    ]

    if args.json:
        print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))
    elif args.csv:
        flat = [flatten_row(r) for r in results]
        writer = csv.DictWriter(sys.stdout, fieldnames=list(flat[0].keys()))
        writer.writeheader()
        writer.writerows(flat)
    elif args.compare or len(results) > 1:
        sys.stdout.write(render_compare_markdown(results))
    else:
        sys.stdout.write(render_single_markdown(results[0]))


if __name__ == "__main__":
    main()
