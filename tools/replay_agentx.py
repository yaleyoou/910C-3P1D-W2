#!/usr/bin/env python3
"""Zero-delay time-boxed replay of AgentX (semianalysisai/cc-traces-weka-062126-256k).

Preserves each root session tree's sequential turn dependencies and parallel
subagent batches while ignoring original tool-call / think delays.
Reconstructs exact-length deterministic token-ID prompts from 64-token hash_ids.
"""
from __future__ import annotations

import argparse
import asyncio
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import statistics
import time
import uuid

import aiohttp


@lru_cache(maxsize=32768)
def token_block(epoch: int, root_idx: int, hash_id: int) -> tuple[int, ...]:
    """Deterministic 64-token block IDs in [1000, 60999], avoiding GLM-5 special IDs."""
    key = ((epoch * 10000 + root_idx) << 32) | hash_id
    prefix = []
    for _ in range(3):
        prefix.append(1000 + key % 60000)
        key //= 60000
    raw = hashlib.shake_256(f'agentx-v1:{epoch}:{root_idx}:{hash_id}'.encode()).digest(122)
    rest = [1000 + int.from_bytes(raw[i:i + 2], 'little') % 60000 for i in range(0, 122, 2)]
    return tuple(prefix + rest)


def build_prompt_ids(epoch: int, root_idx: int, leaf: dict, max_model_len: int) -> tuple[list[int], int]:
    n_in = int(leaf['in'])
    n_out = max(1, int(leaf.get('out', 1)))
    if n_in >= max_model_len:
        n_in = max_model_len - 1
    if n_in + n_out > max_model_len:
        n_out = max(1, max_model_len - n_in)
    hashes = leaf['hash_ids']
    ids = [tok for h in hashes for tok in token_block(epoch, root_idx, int(h))]
    if len(ids) < n_in:
        raise ValueError(f'hash_ids ({len(hashes)} blocks) shorter than in={n_in}')
    return ids[:n_in], n_out


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int((len(s) - 1) * p))]


def summarize_records(records: list[dict], window_s: float) -> dict:
    ttfts = [r['ttft_s'] for r in records if r['ttft_s'] is not None]
    tpots = [r['tpot_ms'] for r in records if r['tpot_ms'] is not None]
    in_toks = sum(r['usage']['prompt_tokens'] for r in records)
    out_toks = sum(r['usage']['completion_tokens'] for r in records)
    return {
        'requests_completed': len(records),
        'input_tokens': in_toks,
        'output_tokens': out_toks,
        'window_s': window_s,
        'output_tokens_per_s': (out_toks / window_s) if window_s > 0 else 0.0,
        'ttft_mean_s': statistics.fmean(ttfts) if ttfts else None,
        'ttft_p50_s': percentile(ttfts, 0.50),
        'ttft_p95_s': percentile(ttfts, 0.95),
        'ttft_p99_s': percentile(ttfts, 0.99),
        'tpot_mean_ms': statistics.fmean(tpots) if tpots else None,
        'tpot_p50_ms': percentile(tpots, 0.50),
        'tpot_p95_ms': percentile(tpots, 0.95),
    }


def iter_roots(trace_path: Path):
    """Lazily yield (epoch, root_idx, trace_dict), cycling cleanly if duration exceeds one pass."""
    epoch = 0
    while True:
        with trace_path.open('r', encoding='utf-8') as f:
            count = 0
            for root_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                count += 1
                yield epoch, root_idx, json.loads(line)
        if count == 0:
            raise ValueError(f'Empty trace file: {trace_path}')
        epoch += 1


async def run_replay(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    events_fp = (out_dir / 'events.jsonl').open('x', encoding='utf-8')

    def log_event(event: str, **kw) -> None:
        events_fp.write(json.dumps({'event': event, 'wall_time': time.time(), **kw}) + '\n')
        events_fp.flush()

    results: list[dict] = []
    errors: list[dict] = []
    running = 0
    peak_inflight = 0
    sent_count = 0
    roots_started = 0
    roots_completed = 0
    t0: float | None = None
    deadline: float | None = None
    seq_counter = 0

    def time_up() -> bool:
        return deadline is not None and time.time() >= deadline

    timeout = aiohttp.ClientTimeout(total=args.request_timeout, sock_read=args.request_timeout)
    connector = aiohttp.TCPConnector(limit=0)
    root_gen = iter_roots(Path(args.traces))
    root_lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as session:
        async def send_leaf(epoch: int, root_idx: int, root_id: str, leaf: dict, branch: str) -> bool:
            nonlocal running, peak_inflight, sent_count, t0, deadline, seq_counter
            if time_up():
                return False
            prompt_ids, target_out = build_prompt_ids(epoch, root_idx, leaf, args.max_model_len)
            seq_counter += 1
            req_id = f'e{epoch}-r{root_idx}-{seq_counter}'
            http_id = str(uuid.uuid4())
            payload = {
                'model': args.model,
                'prompt': prompt_ids,
                'max_tokens': target_out,
                'min_tokens': target_out,
                'ignore_eos': True,
                'temperature': 0,
                'stream': True,
                'stream_options': {'include_usage': True},
            }
            body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            del payload, prompt_ids

            start_unix = time.time()
            if t0 is None:
                t0 = start_unix
                deadline = t0 + args.duration
            elif start_unix >= deadline:
                return False

            perf_start = time.perf_counter()
            first_tok = None
            last_tok = None
            usage = None
            finish_reason = None
            streamed_chunks = 0

            running += 1
            sent_count += 1
            peak_inflight = max(peak_inflight, running)
            log_event(
                'send',
                request_id=req_id,
                http_request_id=http_id,
                root_id=root_id,
                branch=branch,
                expected_prompt_tokens=int(leaf['in']),
                target_output_tokens=target_out,
            )
            try:
                async with session.post(
                    args.url,
                    data=body,
                    headers={'Content-Type': 'application/json', 'X-Request-Id': http_id},
                ) as resp:
                    if resp.status != 200:
                        err_text = (await resp.text())[:1000]
                        raise RuntimeError(f'HTTP {resp.status} {err_text}')
                    async for raw_line in resp.content:
                        if not raw_line.startswith(b'data:'):
                            continue
                        val = raw_line[5:].strip()
                        if not val or val == b'[DONE]':
                            continue
                        chunk = json.loads(val)
                        if chunk.get('usage'):
                            usage = chunk['usage']
                        for choice in chunk.get('choices', []):
                            delta = choice.get('delta') or {}
                            if choice.get('text') or delta.get('content') or delta.get('reasoning_content'):
                                now = time.perf_counter()
                                first_tok = first_tok or now
                                last_tok = now
                                streamed_chunks += 1
                            finish_reason = choice.get('finish_reason') or finish_reason
                end_unix = time.time()
                if usage is None:
                    usage = {
                        'prompt_tokens': int(leaf['in']),
                        'completion_tokens': streamed_chunks or target_out,
                        'total_tokens': int(leaf['in']) + (streamed_chunks or target_out),
                    }
                comp_toks = usage['completion_tokens']
                rec = {
                    'request_id': req_id,
                    'root_id': root_id,
                    'branch': branch,
                    'start_unix': start_unix,
                    'end_unix': end_unix,
                    'latency_s': time.perf_counter() - perf_start,
                    'ttft_s': (first_tok - perf_start) if first_tok is not None else None,
                    'tpot_ms': (
                        1000.0 * (last_tok - first_tok) / (comp_toks - 1)
                        if first_tok is not None and last_tok is not None and comp_toks > 1
                        else None
                    ),
                    'usage': usage,
                    'finish_reason': finish_reason,
                }
                results.append(rec)
                log_event('done', **rec)
                return True
            except asyncio.CancelledError:
                log_event('cancelled', request_id=req_id, root_id=root_id)
                raise
            except Exception as exc:
                err = {'request_id': req_id, 'root_id': root_id, 'error': repr(exc)}
                errors.append(err)
                log_event('error', **err)
                if args.fail_fast:
                    raise
                return False
            finally:
                running -= 1

        async def run_sequence(epoch: int, root_idx: int, root_id: str, items: list[dict], branch: str) -> bool:
            i = 0
            n = len(items)
            while i < n:
                if time_up():
                    return False
                if 'requests' in items[i]:
                    batch = []
                    while i < n and 'requests' in items[i]:
                        batch.append(items[i])
                        i += 1
                    sub_tasks = [
                        asyncio.create_task(
                            run_sequence(
                                epoch,
                                root_idx,
                                root_id,
                                sub['requests'],
                                sub.get('agent_id') or f'{branch}/sub{k}',
                            )
                        )
                        for k, sub in enumerate(batch)
                    ]
                    sub_ok = await asyncio.gather(*sub_tasks)
                    if not all(sub_ok):
                        return False
                else:
                    ok = await send_leaf(epoch, root_idx, root_id, items[i], branch)
                    if not ok:
                        return False
                    i += 1
            return True

        async def worker(slot: int) -> None:
            nonlocal roots_started, roots_completed
            while not time_up():
                async with root_lock:
                    if time_up():
                        break
                    epoch, root_idx, trace = next(root_gen)
                if trace.get('block_size') != 64 or trace.get('hash_id_scope') != 'local':
                    raise ValueError(f"Unsupported trace schema on root {trace.get('id')}")
                roots_started += 1
                root_id = f"{trace['id']}#e{epoch}"
                log_event('root_begin', root_id=root_id, slot=slot)
                completed = await run_sequence(epoch, root_idx, root_id, trace['requests'], 'main')
                if completed:
                    roots_completed += 1
                    log_event('root_done', root_id=root_id, slot=slot)
                else:
                    log_event('root_stopped', root_id=root_id, slot=slot)
                    break

        workers = [asyncio.create_task(worker(s)) for s in range(args.concurrency)]
        try:
            if args.hard_stop:
                await asyncio.sleep(args.duration)
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            else:
                await asyncio.wait_for(asyncio.gather(*workers), timeout=args.duration + args.grace_period)
        except asyncio.TimeoutError:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            events_fp.close()

    if t0 is None:
        raise RuntimeError('No requests were sent')
    assert deadline is not None
    last_done = max((r['end_unix'] for r in results), default=time.time())
    in_window_recs = [r for r in results if r['end_unix'] <= deadline]
    in_window_s = min(args.duration, max(0.0, last_done - t0))
    total_s = max(0.0, last_done - t0)

    report = {
        'dataset': 'semianalysisai/cc-traces-weka-062126-256k',
        'trace_file': str(args.traces),
        'ignore_trace_delays': True,
        'hard_stop': bool(args.hard_stop),
        'configured_duration_s': args.duration,
        'concurrency': args.concurrency,
        'roots_started': roots_started,
        'roots_completed': roots_completed,
        'requests_sent': sent_count,
        'requests_completed': len(results),
        'requests_failed': len(errors),
        'peak_inflight_http': peak_inflight,
        'first_send_unix': t0,
        'deadline_unix': deadline,
        'last_done_unix': last_done,
        'in_window': summarize_records(in_window_recs, in_window_s),
        'total_including_drain': summarize_records(results, total_s),
    }
    (out_dir / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('AGENTX_COMPLETE', json.dumps(report), flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Zero-delay AgentX trace replay for 3P1D vLLM cluster')
    p.add_argument('traces', help='Path to traces.jsonl')
    p.add_argument('out_dir', help='Output directory for events.jsonl and summary.json')
    p.add_argument('--url', default='http://127.0.0.1:8300/v1/completions')
    p.add_argument('--model', default='glm-5')
    p.add_argument('--duration', type=float, default=3600.0, help='Sending duration in seconds')
    p.add_argument('--concurrency', type=int, default=16, help='Concurrent active root session trees')
    p.add_argument('--grace-period', type=float, default=7200.0, help='Max drain seconds after duration')
    p.add_argument('--request-timeout', type=float, default=7200.0)
    p.add_argument('--max-model-len', type=int, default=256000)
    p.add_argument('--hard-stop', action='store_true', help='Cancel inflight requests at duration')
    p.add_argument('--fail-fast', action='store_true', help='Abort immediately on first HTTP error')
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(run_replay(parse_args()))
