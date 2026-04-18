#!/usr/bin/env python3
from __future__ import annotations

import json
import os

import requests


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


ROOT_PATH = env("ADMIN_APP_ROOT_PATH", "/admin")
ADMIN_BASE_URL = env("ADMIN_BASE_URL", f"http://127.0.0.1:{env('NGINX_PORT', '8088')}{ROOT_PATH}")
ADMIN_USERNAME = env("ADMIN_APP_USERNAME", "admin")
ADMIN_PASSWORD = env("ADMIN_APP_PASSWORD", "admin123")


def main() -> int:
    session = requests.Session()
    session.trust_env = False
    login_response = session.post(
        f"{ADMIN_BASE_URL}/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    login_response.raise_for_status()

    pages = {}
    for path in (
        "/services",
        "/monitoring-notifications",
    ):
        response = session.get(f"{ADMIN_BASE_URL}{path}", timeout=20)
        response.raise_for_status()
        pages[path] = response.status_code

    availability_response = session.get(
        f"{ADMIN_BASE_URL}/api/services/availability",
        timeout=20,
    )
    availability_response.raise_for_status()
    availability_payload = availability_response.json()

    channels_response = session.get(
        f"{ADMIN_BASE_URL}/api/monitoring/channels",
        timeout=20,
    )
    channels_response.raise_for_status()
    channels_payload = channels_response.json()

    report = {
        "admin_base_url": ADMIN_BASE_URL,
        "pages": pages,
        "availability": {
            "bucket_minutes": availability_payload.get("bucket_minutes"),
            "poll_interval_seconds": availability_payload.get("poll_interval_seconds"),
            "service_count": len(availability_payload.get("services") or []),
        },
        "channels": {
            "count": len(channels_payload.get("items") or []),
            "secret_ready": channels_payload.get("secret_ready"),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["availability"]["bucket_minutes"] != 10:
        return 1
    if report["availability"]["poll_interval_seconds"] != 60:
        return 1
    if report["availability"]["service_count"] <= 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
