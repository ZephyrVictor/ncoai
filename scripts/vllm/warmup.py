#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ncoai_shared.vllm_replicas import replica_environments


def endpoint_origin(env: dict[str, str]) -> str:
    host = env.get("VLLM_HOST", "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{env.get('VLLM_PORT', '12233')}"


def request_json(url: str, *, payload: dict | None = None, timeout: float = 5.0) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = response.read()
            return json.loads(result) if result else {}
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(str(exc)) from exc


def wait_for_model(origin: str, model: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while True:
        try:
            remaining = max(deadline - time.monotonic(), 0.1)
            request_json(f"{origin}/health", timeout=min(5.0, remaining))
            models = request_json(f"{origin}/v1/models", timeout=min(5.0, remaining))
            names = {entry.get("id") for entry in models.get("data", [])}
            if model not in names:
                raise ValueError(f"served model {model!r} not found; available: {sorted(str(name) for name in names)}")
            return
        except RuntimeError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"vLLM readiness timed out: {last_error}")
        time.sleep(min(1.0, max(deadline - time.monotonic(), 0)))


def warmup(env: dict[str, str], *, ready_timeout: float, request_timeout: float) -> None:
    model = env.get("SERVED_MODEL_NAME", "").strip()
    if not model:
        raise ValueError("SERVED_MODEL_NAME is required")
    origin = endpoint_origin(env)
    wait_for_model(origin, model, timeout=ready_timeout)
    system_prompt = env.get("CHAINLIT_SYSTEM_PROMPT", "You are a concise and helpful assistant.")
    shared = "请根据已知信息简要回答："
    prompts = (
        shared + "你好。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 12 + "请总结要点。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 40 + "请总结要点。",
        shared + "项目背景：" + "需要稳定处理并发请求。" * 12 + "请总结要点。",
    )
    thinking_enabled = env.get("CHAINLIT_ENABLE_THINKING", "1").strip().lower() not in {"0", "false", "no", "off"}
    for index, prompt in enumerate(prompts, 1):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            "max_tokens": 8,
            "temperature": 0,
        }
        if thinking_enabled:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        result = request_json(f"{origin}/v1/chat/completions", payload=payload, timeout=request_timeout)
        if not result.get("choices"):
            raise RuntimeError(f"warmup request {index} returned no choices")
        print(f"[OK] vLLM warmup {index}/{len(prompts)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm the configured vLLM model and shared prompt prefix")
    parser.add_argument("--ready-timeout", type=float, default=float(os.getenv("VLLM_WARMUP_READY_TIMEOUT_SECONDS", "900")))
    parser.add_argument("--request-timeout", type=float, default=float(os.getenv("VLLM_WARMUP_REQUEST_TIMEOUT_SECONDS", "120")))
    args = parser.parse_args()
    if args.ready_timeout <= 0 or args.request_timeout <= 0:
        parser.error("timeouts must be positive")
    try:
        for env in replica_environments(os.environ):
            print(f"[INFO] Warming {endpoint_origin(env)}", flush=True)
            warmup(env, ready_timeout=args.ready_timeout, request_timeout=args.request_timeout)
    except (RuntimeError, ValueError) as exc:
        print(f"[ERROR] vLLM warmup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
