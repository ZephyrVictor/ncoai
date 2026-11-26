#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from ncoai_shared.vllm_replicas import all_vllm_origins, local_replica_origins


def endpoint_origin(env: dict[str, str]) -> str:
    origins = local_replica_origins(env)
    return origins[0] if origins else "http://127.0.0.1:12233"


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float,
    api_key: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key and api_key != "dummy":
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body.strip():
                return {}
            return json.loads(body)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:300]}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{url}: {exc}") from exc


def wait_for_model(
    origin: str,
    model: str,
    *,
    timeout: float,
    api_key: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while True:
        try:
            remaining = max(deadline - time.monotonic(), 0.1)
            req_kw = {"api_key": api_key} if api_key else {}
            request_json(f"{origin}/health", timeout=min(5.0, remaining), **req_kw)
            models = request_json(f"{origin}/v1/models", timeout=min(5.0, remaining), **req_kw)
            names = {entry.get("id") for entry in models.get("data", [])}
            if model not in names:
                raise ValueError(
                    f"served model {model!r} not found at {origin}; available: {sorted(str(name) for name in names)}"
                )
            return
        except RuntimeError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"vLLM readiness timed out for {origin}: {last_error}")
        time.sleep(min(1.0, max(deadline - time.monotonic(), 0)))


def warmup_origin(
    origin: str,
    env: dict[str, str],
    *,
    ready_timeout: float,
    request_timeout: float,
) -> None:
    model = env.get("SERVED_MODEL_NAME", "").strip()
    if not model:
        raise ValueError("SERVED_MODEL_NAME is required")
    api_key = str(env.get("VLLM_API_KEY") or "").strip() or None
    wait_for_model(origin, model, timeout=ready_timeout, api_key=api_key)
    system_prompt = env.get("CHAINLIT_SYSTEM_PROMPT", "You are a concise and helpful assistant.")
    shared = "请根据已知信息简要回答："
    prompts = (
        shared + "你好。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 12 + "请总结要点。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 40 + "请总结要点。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 12 + "请总结要点。",
    )
    for index, prompt in enumerate(prompts, 1):
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req_kw = {"api_key": api_key} if api_key else {}
        result = request_json(
            f"{origin}/v1/chat/completions",
            payload=payload,
            timeout=request_timeout,
            **req_kw,
        )
        if not result.get("choices"):
            raise RuntimeError(f"warmup request {index} at {origin} returned no choices")
        print(f"[OK] {origin} warmup {index}/{len(prompts)}", flush=True)


def warmup(env: dict[str, str], *, ready_timeout: float, request_timeout: float) -> None:
    warmup_origin(
        endpoint_origin(env),
        env,
        ready_timeout=ready_timeout,
        request_timeout=request_timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm all configured local and remote vLLM replicas")
    parser.add_argument("--ready-timeout", type=float, default=float(os.getenv("VLLM_WARMUP_READY_TIMEOUT_SECONDS", "900")))
    parser.add_argument("--request-timeout", type=float, default=float(os.getenv("VLLM_WARMUP_REQUEST_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--local-only", action="store_true", help="Warm only local replicas, skipping VLLM_REMOTE_ORIGINS")
    args = parser.parse_args()
    if args.ready_timeout <= 0 or args.request_timeout <= 0:
        parser.error("timeouts must be positive")
    env = dict(os.environ)
    origins = local_replica_origins(env) if args.local_only else all_vllm_origins(env)
    try:
        if len(origins) <= 1:
            for origin in origins:
                print(f"[INFO] Warming {origin}", flush=True)
                warmup_origin(origin, env, ready_timeout=args.ready_timeout, request_timeout=args.request_timeout)
        else:
            with ThreadPoolExecutor(max_workers=len(origins)) as pool:
                futures = {
                    pool.submit(
                        warmup_origin,
                        origin,
                        env,
                        ready_timeout=args.ready_timeout,
                        request_timeout=args.request_timeout,
                    ): origin
                    for origin in origins
                }
                for fut in as_completed(futures):
                    fut.result()
    except (RuntimeError, ValueError) as exc:
        print(f"[ERROR] vLLM warmup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
