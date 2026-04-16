#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def validate_chainlit_sticky_routing(chat_base_url: str) -> None:
    parsed = urllib.parse.urlsplit(chat_base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid CHAT_BASE_URL: {chat_base_url}")
    root_path = parsed.path.rstrip("/")
    session_id = f"sticky-{time.time_ns()}"
    cookie_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{root_path}/set-session-cookie", "", "")
    )
    health_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{root_path}/health", "", "")
    )
    cookie_header = f"X-Chainlit-Session-id={session_id}"
    payload = json_bytes({"session_id": session_id})

    cookie_request = urllib.request.Request(
        cookie_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_header,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(cookie_request, timeout=10):
        pass

    worker_headers: list[str] = []
    for _ in range(2):
        health_request = urllib.request.Request(
            health_url,
            headers={"Cookie": cookie_header},
            method="GET",
        )
        with opener.open(health_request, timeout=10) as response:
            worker_headers.append(response.headers.get("X-NCOAI-Chainlit-Worker", ""))

    if not worker_headers[0] or worker_headers[0] != worker_headers[1]:
        raise RuntimeError(
            f"Sticky routing validation failed: worker headers={worker_headers}"
        )


def json_bytes(payload: dict[str, str]) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def main() -> int:
    nginx_port = env("NGINX_PORT", "8088")
    env_vars = os.environ.copy()
    env_vars.setdefault("ADMIN_BASE_URL", f"http://127.0.0.1:{nginx_port}/admin")
    env_vars.setdefault("CHAT_BASE_URL", f"http://127.0.0.1:{nginx_port}/chat")
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env_vars.pop(proxy_var, None)
    validate_chainlit_sticky_routing(env_vars["CHAT_BASE_URL"])
    proc = subprocess.run(
        [sys.executable, "scripts/chat_e2e_stream_resume_copy.py"],
        check=False,
        env=env_vars,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
