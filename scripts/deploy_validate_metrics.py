#!/usr/bin/env python3
from __future__ import annotations

import json
import os

import requests


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


PROMETHEUS_URL = env("PROMETHEUS_URL", f"http://127.0.0.1:{env('PROMETHEUS_PORT', '9090')}")
GRAFANA_URL = env("GRAFANA_URL", f"http://127.0.0.1:{env('GRAFANA_PORT', '3001')}")
EXPECTED_JOBS = {
    "prometheus",
    "node-exporter",
    "postgres-exporter",
    "redis-exporter",
    "litellm-metrics",
    "vllm-metrics",
    "host-service-health",
}


def main() -> int:
    http = requests.Session()
    prometheus_response = http.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=20)
    prometheus_response.raise_for_status()
    payload = prometheus_response.json()
    active = payload.get("data", {}).get("activeTargets", [])
    jobs = {item.get("labels", {}).get("job") for item in active}
    grafana_response = http.get(f"{GRAFANA_URL}/login", timeout=20)
    grafana_response.raise_for_status()
    report = {
        "prometheus_url": PROMETHEUS_URL,
        "grafana_url": GRAFANA_URL,
        "active_jobs": sorted(job for job in jobs if job),
        "missing_jobs": sorted(EXPECTED_JOBS - jobs),
        "grafana_login_status": grafana_response.status_code,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["missing_jobs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
