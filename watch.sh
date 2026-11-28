#!/usr/bin/env bash
# Real-time progress monitor for W2 (case.sh) and AgentX (agentx.sh).
# Usage: sudo bash watch.sh [interval_seconds]
set -euo pipefail
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"
interval=${1:-5}
exec python3 - "$interval" <<'PY'
import glob, json, os, sys, time, urllib.request
from pathlib import Path

interval = float(sys.argv[1])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def load_env():
    env = {}
    if os.path.exists("cluster.env"):
        for line in open("cluster.env", encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def check_http(url):
    try:
        with opener.open(url, timeout=2) as r:
            return str(r.status)
    except Exception:
        return "DOWN"

def fmt_dur(sec):
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:d}h{m:02d}m{s:02d}s" if h else f"{m:d}m{s:02d}s"

while True:
    env = load_env()
    cands = sorted(glob.glob("runs/*/*/*/p1"), key=os.path.getmtime)
    d = cands[-1] if cands else None
    lines = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')}  (每 {interval:g}s 刷新，Ctrl+C 退出) ==="]

    checks = []
    for k in ("P1", "P2", "P3"):
        ip = env.get(k, "")
        if ip:
            checks.append(f"{k}({ip}):{check_http(f'http://{ip}:8100/health')}")
    if env.get("D"):
        dip = env["D"]
        checks.append(f"D({dip}):{check_http(f'http://{dip}:8200/health')}")
    if env.get("P1"):
        p1 = env["P1"]
        checks.append(f"Proxy:{check_http(f'http://{p1}:8300/healthcheck')}")
    lines.append("引擎状态: " + "  |  ".join(checks))

    if not d:
        lines.append("当前目录: 暂无 runs/*/*/*/p1")
    else:
        parts = Path(d).parts
        case_name, trial_name, phase = parts[-4], parts[-3], parts[-2]
        lines.append(f"当前运行: 组别={case_name}  trial={trial_name}  阶段={phase}  ({d})")
        ev_w2 = os.path.join(d, "finite-client", "events.jsonl")
        ev_ax = os.path.join(d, "agentx-client", "events.jsonl")
        ev = ev_w2 if os.path.exists(ev_w2) else (ev_ax if os.path.exists(ev_ax) else None)
        if ev:
            sent = done = err = out_tok = 0
            ttfts, tpots = [], []
            t0 = None
            with open(ev, encoding="utf-8") as f:
                for raw in f:
                    try:
                        r = json.loads(raw)
                    except Exception:
                        continue
                    e = r.get("event")
                    if e == "send":
                        sent += 1
                        if t0 is None:
                            t0 = r.get("wall_time")
                    elif e == "done":
                        done += 1
                        out_tok += (r.get("usage") or {}).get("completion_tokens", 0)
                        if r.get("ttft_s") is not None:
                            ttfts.append(r["ttft_s"])
                        if r.get("tpot_ms") is not None:
                            tpots.append(r["tpot_ms"])
                    elif e == "error":
                        err += 1
            elapsed = max(1e-6, time.time() - t0) if t0 else 0.0
            tps = out_tok / elapsed if elapsed > 0 else 0.0
            if ev == ev_w2:
                pct = 100.0 * done / 985.0
                eta = f"  预计剩余 {fmt_dur(elapsed * (985 - done) / done)}" if done > 5 else ""
                lines.append(f"W2 进度 : {done}/985 ({pct:.1f}%) | 在途 {sent - done - err} | 失败 {err} | 已跑 {fmt_dur(elapsed)}{eta}")
            else:
                lines.append(f"AgentX  : 已完成 {done} | 在途 {sent - done - err} | 失败 {err} | 已跑 {fmt_dur(elapsed)}")
            ttft_s = f"{sum(ttfts)/len(ttfts):.2f}s" if ttfts else "-"
            tpot_s = f"{sum(tpots)/len(tpots):.2f}ms" if tpots else "-"
            p95_s = f"{sorted(ttfts)[int((len(ttfts)-1)*0.95)]:.2f}s" if ttfts else "-"
            lines.append(f"实时性能: 输出均速 {tps:.2f} tok/s | TTFT 均值/P95 = {ttft_s} / {p95_s} | TPOT 均值 = {tpot_s}")
        else:
            if phase == "cal":
                lines.append("当前状态: cal 校准阶段（等待引擎就绪或发送 P→D 探针）")
            else:
                lines.append(f"当前状态: {phase} 阶段等待启动中（模型加载 / 图编译 / 等待 Store 段注册）")

        mp = os.path.join(d, "metrics.jsonl")
        if os.path.exists(mp):
            mlines = [x for x in open(mp, encoding="utf-8") if x.strip()]
            if mlines:
                first = json.loads(open(os.path.join(d, "before.json"), encoding="utf-8").read()) if os.path.exists(os.path.join(d, "before.json")) else json.loads(mlines[0])
                m = json.loads(mlines[-1])
                ps = [k for k in ("P1", "P2", "P3") if k in m]
                pq = " ".join(f"{p}={int(m[p]['waiting'])}" for p in ps)
                q_tot = sum(m[p]["query"] - first[p]["query"] for p in ps)
                h_tot = sum((m[p]["local_hit"] - first[p]["local_hit"]) + (m[p]["ext_hit"] - first[p]["ext_hit"]) for p in ps)
                hit_s = f"{100.0*h_tot/q_tot:.1f}%" if q_tot > 0 else "-"
                p_st = sum(m["master"][p + "_segment_allocated_bytes"] for p in ps)
                d_st = m["master"].get("D_segment_allocated_bytes", 0.0)
                reg = m["master"]["registered"]
                lines.append(f"P/D 队列: P等待({pq}) | P前缀命中率 {hit_s} | D容量等待={m['D']['capacity']:.0f} | D逻辑KV={100*m['D']['kv_usage']:.1f}%")
                lines.append(f"Store   : P已用={p_st:.1f} GiB | D已用={d_st:.1f} GiB | 总注册={reg:.1f} GiB")

    sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
    sys.stdout.flush()
    time.sleep(interval)
PY
