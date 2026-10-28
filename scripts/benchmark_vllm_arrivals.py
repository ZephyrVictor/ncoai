#!/usr/bin/env python3
"""Measure a finite, open-loop vLLM arrival burst and its complete drain.

Endpoints must already be running and warmed. This bypasses Chat/LiteLLM;
multiple endpoints receive deterministic round-robin traffic. It measures
offered request rate separately from successful completion throughput.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def distribution(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)

    def percentile(fraction):
        rank = (len(ordered) - 1) * fraction
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)

    return {"mean": statistics.mean(values), "p50": percentile(.5),
            "p95": percentile(.95), "max": max(values), "min": min(values)}


def messages(index: int, seed: int) -> list[dict]:
    rng = random.Random(seed + index * 7919)
    topics = ["capacity", "queueing", "latency", "throughput", "recovery",
              "scheduling", "monitoring", "batching", "memory", "fairness"]
    prompt = (
        f"Request {index}; experiment {rng.randrange(10**9)}. "
        "Explain practical inference serving decisions for this workload: "
        + ", ".join(rng.choices(topics, k=90))
        + ". Give a long numbered technical explanation. Continue until the output limit."
    )
    return [{"role": "system", "content": "You are a precise and practical engineering assistant."},
            {"role": "user", "content": prompt}]


async def request_one(client, endpoint, payload, index, planned, origin, timeout):
    started = time.perf_counter()
    row = {"index": index, "endpoint": endpoint, "ok": False,
           "planned_s": planned - origin, "sent_s": started - origin,
           "dispatch_lag_s": max(started - planned, 0),
           "first_content_s": None, "first_token_s": None}
    done = False
    usage = None
    token_events = []
    try:
        async with asyncio.timeout(timeout), client.stream(
            "POST", endpoint + "/v1/chat/completions", json=payload
        ) as response:
            row["status"] = response.status_code
            if response.status_code != 200:
                raise RuntimeError((await response.aread()).decode(errors="replace")[:500])
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                item = json.loads(data)
                if item.get("error"):
                    raise RuntimeError(str(item["error"]))
                if item.get("usage"):
                    usage = item["usage"]
                for choice in item.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if content or reasoning:
                        now = time.perf_counter()
                        token_events.append(now)
                        if row["first_token_s"] is None:
                            row["first_token_s"] = now - started
                        if content and row["first_content_s"] is None:
                            row["first_content_s"] = now - started
                    if choice.get("finish_reason"):
                        row["finish_reason"] = choice["finish_reason"]
        if not done or not usage or row["first_token_s"] is None:
            raise RuntimeError("Incomplete SSE stream or missing token usage")
        if usage.get("completion_tokens") != payload["max_tokens"]:
            raise RuntimeError(f"Expected {payload['max_tokens']} output tokens; got {usage}")
        row.update(ok=True, usage=usage, total_s=time.perf_counter() - started,
                   completed_s=time.perf_counter() - origin,
                   token_content_events=len(token_events))
        if len(token_events) > 1:
            decode_s = token_events[-1] - token_events[0]
            row["output_token_latency_s"] = decode_s / max(usage["completion_tokens"] - 1, 1)
            row["stream_decode_tokens_per_s"] = (usage["completion_tokens"] - 1) / max(decode_s, 1e-9)
        row["ttft_from_planned_s"] = row["dispatch_lag_s"] + row["first_token_s"]
    except Exception as exc:
        row.update(error=str(exc), total_s=time.perf_counter() - started,
                   completed_s=time.perf_counter() - origin)
    return row


async def monitor(endpoints, stop, samples):
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        while not stop.is_set():
            snapshot = {"utc": datetime.now(timezone.utc).isoformat(), "endpoints": {}}
            for endpoint in endpoints:
                try:
                    response = await client.get(endpoint + "/metrics")
                    response.raise_for_status()
                    selected = {}
                    for line in response.text.splitlines():
                        if line.startswith("#") or not line.strip():
                            continue
                        name, _, value = line.rpartition(" ")
                        metric = name.split("{")[0]
                        if metric in {"vllm:num_requests_running", "vllm:num_requests_waiting",
                                      "vllm:num_preemptions_total", "vllm:kv_cache_usage_perc",
                                      "vllm:gpu_cache_usage_perc"}:
                            selected[metric] = selected.get(metric, 0) + float(value)
                    snapshot["endpoints"][endpoint] = selected
                except Exception as exc:
                    snapshot["endpoints"][endpoint] = {"error": str(exc)}
            samples.append(snapshot)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass


async def benchmark(args):
    started_utc = datetime.now(timezone.utc).isoformat()
    endpoints = [endpoint.rstrip("/") for endpoint in args.endpoints]
    payloads = [{"model": args.model, "messages": messages(i, args.seed),
                 "temperature": 0, "max_tokens": args.output_tokens,
                 "ignore_eos": True, "stream": True,
                 "chat_template_kwargs": {"enable_thinking": False},
                 "stream_options": {"include_usage": True}} for i in range(args.requests)]
    rows, samples = [], []
    stop = asyncio.Event()
    monitor_task = asyncio.create_task(monitor(endpoints, stop, samples))
    limits = httpx.Limits(max_connections=args.requests + len(endpoints),
                          max_keepalive_connections=args.requests)
    try:
        async with httpx.AsyncClient(timeout=args.timeout, limits=limits, trust_env=False) as client:
            # Establish local connections before the arrival clock begins.
            for endpoint in endpoints:
                (await client.get(endpoint + "/health")).raise_for_status()
            origin = time.perf_counter()
            tasks = []
            for i, payload in enumerate(payloads):
                planned = origin + i / args.qps
                await asyncio.sleep(max(planned - time.perf_counter(), 0))
                tasks.append(asyncio.create_task(request_one(
                    client, endpoints[i % len(endpoints)], payload, i, planned, origin, args.timeout)))
            for task in asyncio.as_completed(tasks):
                rows.append(await task)
                if len(rows) % 20 == 0 or len(rows) == args.requests:
                    print(json.dumps({"completed": len(rows), "total": args.requests,
                                      "success": sum(r["ok"] for r in rows),
                                      "elapsed_s": round(time.perf_counter() - origin, 2)}), flush=True)
            wall = time.perf_counter() - origin
    finally:
        stop.set()
        await monitor_task
    good = [row for row in rows if row["ok"]]
    summary = {"label": args.label, "offered_qps": args.qps, "requests": args.requests,
               "arrival_window_s": (args.requests - 1) / args.qps,
               "success": len(good), "failed": len(rows) - len(good),
               "wall_s": wall, "completed_qps": len(good) / wall,
               "output_tokens_per_s": sum(row["usage"]["completion_tokens"] for row in good) / wall,
               "endpoints": endpoints, "distribution": "deterministic round-robin",
               "scope": "direct vLLM SSE, finite arrival burst plus full drain; excludes Chat/LiteLLM"}
    for metric in ["first_token_s", "first_content_s", "total_s", "dispatch_lag_s",
                   "ttft_from_planned_s", "stream_decode_tokens_per_s", "output_token_latency_s"]:
        summary[metric] = distribution([row[metric] for row in good if row.get(metric) is not None])
    summary["prompt_tokens"] = distribution([r["usage"]["prompt_tokens"] for r in good])
    summary["completion_tokens"] = distribution([r["usage"]["completion_tokens"] for r in good])
    summary["actual_dispatch_span_s"] = max(r["sent_s"] for r in rows) - min(r["sent_s"] for r in rows)
    summary["per_endpoint"] = {ep: {"requests": sum(r["endpoint"] == ep for r in rows),
                                    "success": sum(r["endpoint"] == ep and r["ok"] for r in rows)}
                                for ep in endpoints}
    report = {"started_utc": started_utc,
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "parameters": vars(args), "summary": summary,
              "requests": sorted(rows, key=lambda r: r["index"]), "metrics": samples}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--endpoints", nargs="+", required=True)
    result.add_argument("--model", default="qwen35_27b")
    result.add_argument("--qps", type=float, default=80)
    result.add_argument("--requests", type=int, default=160)
    result.add_argument("--output-tokens", type=int, default=512)
    result.add_argument("--seed", type=int, default=20261028)
    result.add_argument("--timeout", type=float, default=600)
    result.add_argument("--label", default="vllm")
    result.add_argument("--output", required=True)
    return result


if __name__ == "__main__":
    command_parser = parser()
    arguments = command_parser.parse_args()
    if min(arguments.qps, arguments.requests, arguments.output_tokens, arguments.timeout) <= 0:
        command_parser.error("QPS, request count, output tokens and timeout must be positive")
    outcome = asyncio.run(benchmark(arguments))
    raise SystemExit(1 if outcome["summary"]["failed"] else 0)
