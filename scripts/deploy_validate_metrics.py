#!/usr/bin/env python3
from __future__ import annotations

import json
import os

import requests


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


PROMETHEUS_URL = env("PROMETHEUS_URL", f"http://127.0.0.1:{env('PROMETHEUS_PORT', '9090')}")
GRAFANA_ROOT_PATH = env("GRAFANA_ROOT_PATH", "/grafana/").rstrip("/")
GRAFANA_URL = env(
    "GRAFANA_URL",
    f"http://127.0.0.1:{env('NGINX_PORT', '8088')}{GRAFANA_ROOT_PATH}",
)
LOKI_URL = env("LOKI_URL", f"http://127.0.0.1:{env('LOKI_PORT', '3101')}")
GRAFANA_ADMIN_USER = env("GRAFANA_ADMIN_USER", "admin")
GRAFANA_ADMIN_PASSWORD = env("GRAFANA_ADMIN_PASSWORD", "admin")
EXPECTED_JOBS = {
    "prometheus",
    "node-exporter",
    "postgres-exporter",
    "redis-exporter",
    "admin-metrics",
    "chainlit-metrics",
    "litellm-metrics",
    "vllm-metrics",
    "host-service-health",
}
EXPECTED_DATASOURCES = {"Prometheus", "Loki"}
EXPECTED_DASHBOARDS = {
    "Ops Overview",
    "Host & Infra",
    "LLM Runtime",
    "Logs & Errors",
}


def main() -> int:
    http = requests.Session()
    prometheus_response = http.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=20)
    prometheus_response.raise_for_status()
    payload = prometheus_response.json()
    active = payload.get("data", {}).get("activeTargets", [])
    jobs = {item.get("labels", {}).get("job") for item in active}
    grafana_response = http.get(f"{GRAFANA_URL.rstrip('/')}/login", timeout=20)
    grafana_response.raise_for_status()
    loki_response = http.get(f"{LOKI_URL}/ready", timeout=20)
    loki_response.raise_for_status()
    datasources_response = http.get(
        f"{GRAFANA_URL.rstrip('/')}/api/datasources",
        timeout=20,
        auth=(GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD),
    )
    datasources_response.raise_for_status()
    datasources = {
        item.get("name")
        for item in datasources_response.json()
        if item.get("name")
    }
    dashboards_response = http.get(
        f"{GRAFANA_URL.rstrip('/')}/api/search",
        timeout=20,
        auth=(GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD),
    )
    dashboards_response.raise_for_status()
    dashboards = {
        item.get("title")
        for item in dashboards_response.json()
        if item.get("title")
    }
    report = {
        "prometheus_url": PROMETHEUS_URL,
        "grafana_url": GRAFANA_URL,
        "loki_url": LOKI_URL,
        "active_jobs": sorted(job for job in jobs if job),
        "missing_jobs": sorted(EXPECTED_JOBS - jobs),
        "grafana_login_status": grafana_response.status_code,
        "loki_ready_status": loki_response.status_code,
        "grafana_datasources": sorted(datasource for datasource in datasources if datasource),
        "missing_datasources": sorted(EXPECTED_DATASOURCES - datasources),
        "grafana_dashboards": sorted(dashboard for dashboard in dashboards if dashboard),
        "missing_dashboards": sorted(EXPECTED_DASHBOARDS - dashboards),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["missing_jobs"] and not report["missing_datasources"] and not report["missing_dashboards"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
