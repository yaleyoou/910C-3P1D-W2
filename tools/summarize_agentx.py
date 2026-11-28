#!/usr/bin/env python3
"""Summarize one or more AgentX 3600s replay runs with P/D queue, hit, and Store metrics."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding='utf-8'))


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]


def time_mean(samples: list[dict], lo: float, hi: float, fn) -> float:
    area = 0.0
    covered = 0.0
    for x, y in zip(samples, samples[1:]):
        dt = min(hi, y['time']) - max(lo, x['time'])
        if dt > 0:
            area += dt * fn(x)
            covered += dt
    return area / covered if covered > 0 else 0.0


def sample_at_or_after(samples: list[dict], ts: float) -> dict:
    for s in samples:
        if s['time'] >= ts:
            return s
    return samples[-1]


def sample_at_or_before(samples: list[dict], ts: float) -> dict:
    chosen = samples[0]
    for s in samples:
        if s['time'] <= ts:
            chosen = s
        else:
            break
    return chosen


def summarize_run(run_dir: Path, scope: str = 'total', details_path: Path | None = None) -> dict:
    s = read_json(run_dir / 'agentx-client/summary.json')
    g = read_json(run_dir / 'params.json')
    before = read_json(run_dir / 'before.json')
    after = read_json(run_dir / 'after.json')
    samples = [before] + read_jsonl(run_dir / 'metrics.jsonl') + [after]

    if scope == 'window':
        bucket = s['in_window']
        lo = s['first_send_unix']
        hi = min(s['deadline_unix'], s['last_done_unix'])
        s_lo = sample_at_or_before(samples, lo)
        s_hi = sample_at_or_after(samples, hi)
    else:
        bucket = s['total_including_drain']
        lo = s['first_send_unix']
        hi = s['last_done_unix']
        s_lo = samples[0]
        s_hi = samples[-1]

    ps = [p for p in ('P1', 'P2', 'P3') if p in before]
    avg = lambda fn: time_mean(samples, lo, hi, fn)
    q_total = sum(s_hi[p]['query'] - s_lo[p]['query'] for p in ps)
    h_total = sum((s_hi[p]['local_hit'] - s_lo[p]['local_hit']) + (s_hi[p]['ext_hit'] - s_lo[p]['ext_hit']) for p in ps)
    hit_pct = (100.0 * h_total / q_total) if q_total > 0 else 0.0

    per_p = {
        p: {
            'query': s_hi[p]['query'] - s_lo[p]['query'],
            'local_hit': s_hi[p]['local_hit'] - s_lo[p]['local_hit'],
            'ext_hit': s_hi[p]['ext_hit'] - s_lo[p]['ext_hit'],
            'queue': avg(lambda x, p=p: x[p]['waiting']),
            'store_GiB': avg(lambda x, p=p: x['master'][p + '_segment_allocated_bytes']),
        }
        for p in ps
    }
    if details_path is not None:
        details_path.write_text(json.dumps(per_p, indent=2) + '\n', encoding='utf-8')

    in_win = s['in_window']
    p_count = g.get('p_count', len(ps))
    d_dp = g.get('d_dp', 1)
    sparse_label = f"{g['sparse']}×{d_dp}" if d_dp > 1 else f"{g['sparse']}"
    return {
        '组别': g['case'],
        'D Store / sparse（GiB）': f"{g['ds']} / {sparse_label}",
        '总预算 GiB': g['ps'] * p_count + g['ds'] + g['sparse'] * d_dp,
        '并发 c': s['concurrency'],
        '时长 s': int(round(s['configured_duration_s'])),
        '3600s内完成请求': in_win['requests_completed'],
        '含收尾完成请求': s['requests_completed'],
        '失败请求': s['requests_failed'],
        '输出 tok/s': bucket['output_tokens_per_s'],
        'TTFT 均值 s': bucket['ttft_mean_s'] or 0.0,
        'TTFT P95 s': bucket['ttft_p95_s'] or 0.0,
        '平均 TPOT ms': bucket['tpot_mean_ms'] or 0.0,
        'P 平均排队数': sum(v['queue'] for v in per_p.values()),
        'D 平均容量等待数': avg(lambda x: x['D']['capacity']),
        'P 合计前缀命中率 %': hit_pct,
        'P Store 平均占用 GiB': sum(v['store_GiB'] for v in per_p.values()),
        'D Store 平均占用 GiB': avg(lambda x: x['master']['D_segment_allocated_bytes']),
        'D 逻辑 KV 平均使用率 %': 100.0 * avg(lambda x: x['D']['kv_usage']),
    }


def format_markdown(rows: list[dict]) -> str:
    headers = [
        '组别', 'D Store / sparse（GiB）', '总预算 GiB', '并发', '时长（s）',
        '窗内完成请求', '含收尾完成请求', '失败', '输出 tok/s', 'TTFT 均值 / P95（s）',
        '平均 TPOT（ms）', 'P 平均排队数', 'D 平均容量等待数', 'P 合计前缀命中率',
        'P Store 平均占用（GiB）', 'D Store 平均占用（GiB）', 'D 逻辑 KV 平均使用率',
    ]
    lines = [
        '# AgentX 零延迟回放结果汇总',
        '',
        '| ' + ' | '.join(headers) + ' |',
        '| --- | ' + ' | '.join(['---:'] * (len(headers) - 1)) + ' |',
    ]
    for r in rows:
        cells = [
            str(r['组别']),
            str(r['D Store / sparse（GiB）']),
            str(r['总预算 GiB']),
            str(r['并发 c']),
            str(r['时长 s']),
            f"{r['3600s内完成请求']:,}",
            f"{r['含收尾完成请求']:,}",
            str(r['失败请求']),
            f"{r['输出 tok/s']:.2f}",
            f"{r['TTFT 均值 s']:.2f} / {r['TTFT P95 s']:.2f}",
            f"{r['平均 TPOT ms']:.2f}",
            f"{r['P 平均排队数']:.2f}",
            f"{r['D 平均容量等待数']:.2f}",
            f"{r['P 合计前缀命中率 %']:.2f}%",
            f"{r['P Store 平均占用 GiB']:.1f}",
            f"{r['D Store 平均占用 GiB']:.1f}",
            f"{r['D 逻辑 KV 平均使用率 %']:.1f}%",
        ]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('runs', type=Path, nargs='+', help='Run directories (e.g. runs/agentx-F1/r1/perf/p1)')
    parser.add_argument('--scope', choices=['total', 'window'], default='total',
                        help='Compute throughput/latency/metrics over total (including drain) or strict duration window')
    parser.add_argument('--details', type=Path)
    parser.add_argument('--markdown', action='store_true')
    args = parser.parse_args()
    if args.details and len(args.runs) != 1:
        parser.error('--details takes one run')
    rows = [summarize_run(p, scope=args.scope, details_path=args.details) for p in args.runs]
    if args.markdown:
        sys.stdout.write(format_markdown(rows))
    else:
        out = csv.DictWriter(sys.stdout, rows[0].keys())
        out.writeheader()
        out.writerows([{k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in rows])
