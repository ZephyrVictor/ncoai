#!/usr/bin/env python3
import argparse
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

EST_TOKENS_PER_WORD = 1.3

PROMPT_POOL = [
    "Analyze capacity, queueing, latency, and failure recovery under heavy traffic.",
    "Use a structured format with assumptions, trade-offs, and measurable outcomes.",
    "Explain practical tuning steps and expected impact on throughput and p95 latency.",
    "Include rollback conditions and risk controls for production stability.",
    "Provide concise metrics-first reasoning and avoid vague statements.",
    "Compare conservative and aggressive parameter settings with clear guardrails.",
    "List bottleneck signals and the fastest way to verify each hypothesis.",
    "Describe observability requirements needed for safe optimization.",
    "Keep recommendations implementation-ready for SRE and platform teams.",
    "Add a one-hour validation checklist with pass or fail criteria.",
]


@dataclass
class Result:
    ok: bool
    status: int
    latency_s: float
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    error: Optional[str]


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    arr = sorted(values)
    if len(arr) == 1:
        return arr[0]
    rank = (len(arr) - 1) * p
    low = int(rank)
    high = min(low + 1, len(arr) - 1)
    frac = rank - low
    return arr[low] * (1 - frac) + arr[high] * frac


def make_prompt(seed: int, idx: int, target_prompt_tokens: int) -> str:
    rng = random.Random(seed + idx * 7919)
    target_words = max(40, int(target_prompt_tokens / EST_TOKENS_PER_WORD))
    words: List[str] = []
    intro = (
        f"Task {idx}: You are a senior inference platform engineer. "
        "Assess high-concurrency capacity and provide practical optimization steps."
    )
    words.extend(intro.split())
    while len(words) < target_words:
        words.extend(rng.choice(PROMPT_POOL).split())

    tail = (
        "\n\nReturn roughly 880 tokens with numbered sections: settings, risks, validation."
    )
    return " ".join(words[:target_words]) + tail


def send_one(
    idx: int,
    url: str,
    model: str,
    timeout_s: float,
    seed: int,
    target_prompt_tokens: int,
    max_tokens: int,
    api_key: str,
) -> Result:
    prompt = make_prompt(seed=seed, idx=idx, target_prompt_tokens=target_prompt_tokens)
    payload: Dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a precise and practical engineering assistant."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.6,
        "top_p": 0.9,
        "max_tokens": max_tokens,
    }

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            status = resp.getcode()
            obj = json.loads(body)
            usage = obj.get("usage", {}) if isinstance(obj, dict) else {}
            return Result(
                ok=(200 <= status < 300),
                status=status,
                latency_s=elapsed,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                error=None,
            )
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - started
        try:
            err = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err = str(e)
        return Result(
            ok=False,
            status=e.code,
            latency_s=elapsed,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            error=err,
        )
    except Exception as e:
        elapsed = time.perf_counter() - started
        return Result(
            ok=False,
            status=0,
            latency_s=elapsed,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            error=str(e),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="100并发、约1000 token/请求压测")
    parser.add_argument("--url", default="http://127.0.0.1:4000/v1/chat/completions")
    parser.add_argument("--model", default="qwen35b_no_think")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--target-total-tokens", type=int, default=1000)
    parser.add_argument("--target-prompt-tokens", type=int, default=120)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--save-json", default="")
    args = parser.parse_args()

    if args.target_total_tokens <= args.target_prompt_tokens + 64:
        raise ValueError("target_total_tokens 太小，请至少比 target_prompt_tokens 大 64")

    max_tokens = args.target_total_tokens - args.target_prompt_tokens

    print("=== Load Test Config ===")
    print(f"url={args.url}")
    print(f"model={args.model}")
    print(f"concurrency={args.concurrency}, requests={args.requests}")
    print(
        f"target_total_tokens~{args.target_total_tokens}, "
        f"target_prompt_tokens~{args.target_prompt_tokens}, max_tokens={max_tokens}"
    )

    started = time.perf_counter()
    results: List[Result] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [
            pool.submit(
                send_one,
                i,
                args.url,
                args.model,
                args.timeout,
                args.seed,
                args.target_prompt_tokens,
                max_tokens,
                args.api_key,
            )
            for i in range(args.requests)
        ]

        for i, fut in enumerate(as_completed(futs), start=1):
            r = fut.result()
            results.append(r)
            if i % max(1, args.requests // 10) == 0 or i == args.requests:
                ok = sum(1 for x in results if x.ok)
                print(f"progress={i}/{args.requests}, success={ok}, fail={i-ok}")

    wall = time.perf_counter() - started

    ok_rows = [x for x in results if x.ok]
    fail_rows = [x for x in results if not x.ok]
    latencies = [x.latency_s for x in ok_rows]

    p_tokens = [x.prompt_tokens for x in ok_rows if x.prompt_tokens is not None]
    c_tokens = [x.completion_tokens for x in ok_rows if x.completion_tokens is not None]
    t_tokens = [x.total_tokens for x in ok_rows if x.total_tokens is not None]

    print("\n=== Summary ===")
    print(f"total_requests={len(results)}")
    print(f"success={len(ok_rows)}, failed={len(fail_rows)}")
    print(f"wall_time_s={wall:.3f}")
    print(f"throughput_req_s={len(results)/wall:.3f}")

    if latencies:
        print(f"latency_avg_s={statistics.mean(latencies):.3f}")
        print(f"latency_p50_s={percentile(latencies, 0.50):.3f}")
        print(f"latency_p90_s={percentile(latencies, 0.90):.3f}")
        print(f"latency_p95_s={percentile(latencies, 0.95):.3f}")
        print(f"latency_p99_s={percentile(latencies, 0.99):.3f}")

    if p_tokens:
        print(f"avg_prompt_tokens={statistics.mean(p_tokens):.1f}")
    else:
        print("avg_prompt_tokens=NA")

    if c_tokens:
        total_c = sum(c_tokens)
        print(f"avg_completion_tokens={statistics.mean(c_tokens):.1f}")
        print(f"completion_tokens_total={total_c}")
        print(f"throughput_output_tokens_s={total_c / wall:.1f}")
    else:
        print("avg_completion_tokens=NA")

    if t_tokens:
        total_t = sum(t_tokens)
        print(f"avg_total_tokens={statistics.mean(t_tokens):.1f}")
        print(f"total_tokens={total_t}")
        print(f"throughput_total_tokens_s={total_t / wall:.1f}")
    else:
        print("avg_total_tokens=NA")

    if fail_rows:
        print("\n=== Failure Samples (up to 10) ===")
        for r in fail_rows[:10]:
            err = (r.error or "").replace("\n", " ")
            print(f"status={r.status}, latency_s={r.latency_s:.3f}, error={err[:220]}")

    if args.save_json:
        rows = [
            {
                "ok": r.ok,
                "status": r.status,
                "latency_s": r.latency_s,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "error": r.error,
            }
            for r in results
        ]
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"saved_json={args.save_json}")


if __name__ == "__main__":
    main()
