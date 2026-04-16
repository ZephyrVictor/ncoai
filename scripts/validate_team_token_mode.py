#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_text(url: str, *, method: str = "GET", data: bytes | None = None) -> tuple[int, dict[str, str], str]:
    opener = build_opener()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with opener.open(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = {key: value for key, value in response.headers.items()}
            return response.status, headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        headers = {key: value for key, value in exc.headers.items()}
        return exc.code, headers, body


def assert_team_token_login_page(url: str) -> dict[str, Any]:
    status, headers, body = read_text(url)
    if status != 200:
        raise RuntimeError(f"{url} returned {status}")
    if "Team token" not in body and "teamToken" not in body:
        raise RuntimeError(f"{url} did not render the team-token login form")
    return {
        "url": url,
        "status": status,
        "worker": headers.get("x-ncoai-chainlit-worker", ""),
    }


def assert_invalid_team_token(url: str, team_token: str) -> dict[str, Any]:
    status, headers, body = read_text(
        url,
        method="POST",
        data=json.dumps({"teamToken": team_token}).encode("utf-8"),
    )
    payload: dict[str, Any]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body}
    if status >= 500:
        raise RuntimeError(f"{url} returned unexpected {status}: {body}")
    if status not in {400, 401}:
        raise RuntimeError(f"{url} returned unexpected {status}: {body}")
    return {
        "url": url,
        "status": status,
        "detail": payload.get("detail"),
        "worker": headers.get("x-ncoai-chainlit-worker", ""),
    }


def maybe_validate_real_team_token(url: str, team_token: str) -> dict[str, Any]:
    status, headers, body = read_text(
        url,
        method="POST",
        data=json.dumps({"teamToken": team_token, "next": "/chat"}).encode("utf-8"),
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} returned non-JSON body for real token: {body}") from exc
    if status != 200:
        raise RuntimeError(f"{url} rejected TEAM_TOKEN_PROBE_VALUE with {status}: {body}")
    if not payload.get("success"):
        raise RuntimeError(f"{url} did not report success for TEAM_TOKEN_PROBE_VALUE: {body}")
    if "access_token=" not in headers.get("set-cookie", ""):
        raise RuntimeError(f"{url} did not set an auth cookie for TEAM_TOKEN_PROBE_VALUE")
    return {
        "url": url,
        "status": status,
        "redirect_to": payload.get("redirectTo"),
        "identifier": (payload.get("user") or {}).get("identifier"),
        "worker": headers.get("x-ncoai-chainlit-worker", ""),
    }


def worker_urls(base_port: int, worker_count: int) -> list[str]:
    return [f"http://127.0.0.1:{base_port + index}/chat" for index in range(worker_count)]


def main() -> int:
    nginx_port = int(env("NGINX_PORT", "8088"))
    worker_count = int(env("CHAINLIT_WORKER_COUNT", "4"))
    worker_base_port = int(env("CHAINLIT_WORKER_BASE_PORT", env("CHAINLIT_PORT", "3100")))
    team_token_probe_value = env("TEAM_TOKEN_PROBE_VALUE", "")

    chat_base = env("CHAT_BASE_URL", f"http://127.0.0.1:{nginx_port}/chat").rstrip("/")
    login_url = f"{chat_base}/login"
    login_api_url = f"{chat_base}/api/team-token/login"

    result: dict[str, Any] = {
        "chat_base_url": chat_base,
        "worker_count": worker_count,
        "worker_base_port": worker_base_port,
        "login_page": assert_team_token_login_page(login_url),
        "workers": [],
        "invalid_signature_probe": assert_invalid_team_token(login_api_url, "17:not-base64!!"),
        "invalid_signature_but_base64_probe": assert_invalid_team_token(
            login_api_url,
            "17:ZmFrZS1zaWduYXR1cmU=",
        ),
    }

    for worker_chat_base in worker_urls(worker_base_port, worker_count):
        worker_login_url = f"{worker_chat_base}/login"
        worker_login_api_url = f"{worker_chat_base}/api/team-token/login"
        worker_result = {
            "login_page": assert_team_token_login_page(worker_login_url),
            "invalid_signature_probe": assert_invalid_team_token(
                worker_login_api_url,
                "17:not-base64!!",
            ),
        }
        result["workers"].append(worker_result)

    if team_token_probe_value:
        result["real_token_probe"] = maybe_validate_real_team_token(
            login_api_url,
            team_token_probe_value,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
