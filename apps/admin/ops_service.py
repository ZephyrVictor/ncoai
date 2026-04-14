from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


RESOURCE_QUERIES = {
    "cpu_percent": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
    "memory_percent": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
    "disk_percent": '100 * (1 - (node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}))',
}

SERVICE_JOB_LABELS = (
    ("prometheus", "Prometheus"),
    ("node-exporter", "Node Exporter"),
    ("postgres-exporter", "PostgreSQL Exporter"),
    ("redis-exporter", "Redis Exporter"),
    ("admin-metrics", "Admin Metrics"),
    ("chainlit-metrics", "Chainlit Metrics"),
    ("litellm-metrics", "LiteLLM Metrics"),
    ("vllm-metrics", "vLLM Metrics"),
)

PROBE_MATCHERS = (
    ("/chat/health", "Chainlit Health"),
    ("/admin/healthz", "Admin Health"),
    ("33455/metrics", "LiteLLM Probe"),
    ("12233/metrics", "vLLM Probe"),
)
OPS_LOG_SERVICES = ("admin", "chainlit", "litellm", "vllm")
OPS_LOG_DEFAULT_LEVELS = ("error", "critical", "fatal")
OPS_LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serialize_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _join_url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    base = base_url.rstrip("/")
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{base}{path}{suffix}"


def _request_json(url: str, *, timeout: int = 15) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - operational path
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}".strip()) from exc
    except URLError as exc:  # pragma: no cover - operational path
        raise RuntimeError(str(exc.reason)) from exc
    return json.loads(body)


async def fetch_json(url: str, *, timeout: int = 15) -> dict[str, Any]:
    return await asyncio.to_thread(_request_json, url, timeout=timeout)


async def query_prometheus(prometheus_url: str, expression: str) -> list[dict[str, Any]]:
    payload = await fetch_json(
        _join_url(
            prometheus_url,
            "/api/v1/query",
            {"query": expression},
        )
    )
    return payload.get("data", {}).get("result", [])


def first_result_float(results: list[dict[str, Any]]) -> float | None:
    if not results:
        return None
    value = results[0].get("value")
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def summarize_target_group(targets: list[dict[str, Any]]) -> tuple[str, str]:
    if not targets:
        return "missing", "No scrape target registered."

    health_values = [str(item.get("health") or "unknown").lower() for item in targets]
    if all(value == "up" for value in health_values):
        return "up", "All targets are healthy."
    if any(value == "up" for value in health_values):
        return "degraded", "Some targets are healthy, some are failing."

    errors = [str(item.get("lastError") or "").strip() for item in targets]
    error_message = next((item for item in errors if item), "All targets are failing.")
    return "down", error_message


def flatten_probe_targets(targets: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for suffix, label in PROBE_MATCHERS:
        matched = [
            item
            for item in targets
            if suffix in str(item.get("labels", {}).get("instance") or "")
        ]
        status, detail = summarize_target_group(matched)
        items.append(
            {
                "name": label,
                "status": status,
                "detail": detail,
            }
        )
    return items


async def load_ops_summary(
    *,
    prometheus_url: str,
    grafana_url: str,
) -> dict[str, Any]:
    errors: list[str] = []
    resources: dict[str, float | None] = {}
    services: list[dict[str, str]] = []
    health_checks: list[dict[str, str]] = []
    firing_count = 0
    pending_count = 0

    try:
        targets_payload = await fetch_json(_join_url(prometheus_url, "/api/v1/targets"))
        active_targets = targets_payload.get("data", {}).get("activeTargets", [])
        for job_name, label in SERVICE_JOB_LABELS:
            matched = [
                item
                for item in active_targets
                if str(item.get("labels", {}).get("job") or "") == job_name
            ]
            status, detail = summarize_target_group(matched)
            services.append({"name": label, "status": status, "detail": detail})

        probe_targets = [
            item
            for item in active_targets
            if str(item.get("labels", {}).get("job") or "") == "host-service-health"
        ]
        health_checks = flatten_probe_targets(probe_targets)
    except Exception as exc:  # pragma: no cover - operational path
        errors.append(f"Prometheus targets unavailable: {exc}")

    for key, expression in RESOURCE_QUERIES.items():
        try:
            resources[key] = first_result_float(await query_prometheus(prometheus_url, expression))
        except Exception as exc:  # pragma: no cover - operational path
            resources[key] = None
            errors.append(f"Prometheus query failed for {key}: {exc}")

    try:
        alerts_payload = await fetch_json(_join_url(prometheus_url, "/api/v1/alerts"))
        alerts = alerts_payload.get("data", {}).get("alerts", [])
        firing_count = sum(1 for item in alerts if str(item.get("state")).lower() == "firing")
        pending_count = sum(1 for item in alerts if str(item.get("state")).lower() == "pending")
    except Exception as exc:  # pragma: no cover - operational path
        errors.append(f"Prometheus alerts unavailable: {exc}")

    return {
        "ok": not errors,
        "generated_at": serialize_datetime(utcnow()),
        "resources": resources,
        "services": services,
        "health_checks": health_checks,
        "alerts": {
            "firing_count": firing_count,
            "pending_count": pending_count,
        },
        "links": {
            "grafana": grafana_url,
            "overview_dashboard": f"{grafana_url.rstrip('/')}/d/ops-overview/ops-overview",
            "logs_dashboard": f"{grafana_url.rstrip('/')}/d/logs-errors/logs-errors",
        },
        "errors": errors,
    }


async def load_ops_alerts(*, prometheus_url: str) -> dict[str, Any]:
    try:
        payload = await fetch_json(_join_url(prometheus_url, "/api/v1/alerts"))
        raw_items = payload.get("data", {}).get("alerts", [])
        items = [
            {
                "name": str(item.get("labels", {}).get("alertname") or "UnknownAlert"),
                "state": str(item.get("state") or "unknown").lower(),
                "severity": str(item.get("labels", {}).get("severity") or "warning").lower(),
                "summary": str(item.get("annotations", {}).get("summary") or ""),
                "description": str(item.get("annotations", {}).get("description") or ""),
                "active_at": str(item.get("activeAt") or ""),
            }
            for item in raw_items
        ]
        items.sort(key=lambda item: (item["state"] != "firing", item["name"]))
        return {
            "ok": True,
            "generated_at": serialize_datetime(utcnow()),
            "items": items,
        }
    except Exception as exc:  # pragma: no cover - operational path
        return {
            "ok": False,
            "generated_at": serialize_datetime(utcnow()),
            "items": [],
            "error": str(exc),
        }


def _split_csv_filter(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_label_regex(values: list[str]) -> str:
    return "|".join(re.escape(value) for value in values)


def _filter_ops_log_services(service: str | None) -> list[str]:
    requested = _split_csv_filter(service)
    if not requested:
        return list(OPS_LOG_SERVICES)
    allowed = set(OPS_LOG_SERVICES)
    return [item for item in requested if item in allowed]


def _filter_ops_log_levels(level: str | None) -> list[str]:
    requested = _split_csv_filter(level)
    if requested:
        return requested
    return list(OPS_LOG_DEFAULT_LEVELS)


def _ops_log_window(*, hours: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or utcnow()
    local_end = end.astimezone(OPS_LOG_TIMEZONE)
    local_start_of_day = local_end.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day = local_start_of_day.astimezone(timezone.utc)
    lookback_start = end - timedelta(hours=max(hours, 1))
    return max(start_of_day, lookback_start), end


def build_logs_query(
    *,
    service: str | None = None,
    level: str | None = None,
    text_query: str | None = None,
) -> str:
    labels = ['job="host-services"']

    service_values = _filter_ops_log_services(service)
    requested_services = _split_csv_filter(service)
    if requested_services and not service_values:
        labels.append('service=~"^$"')
    else:
        labels.append(f'service=~"{_build_label_regex(service_values)}"')

    level_values = _filter_ops_log_levels(level)
    labels.append(f'level=~"{_build_label_regex(level_values)}"')

    query = "{" + ", ".join(labels) + "}"
    if text_query:
        query += f" |= {json.dumps(text_query)}"
    return query


async def load_ops_logs(
    *,
    loki_url: str,
    service: str | None = None,
    level: str | None = None,
    text_query: str | None = None,
    hours: int = 24,
    limit: int = 40,
) -> dict[str, Any]:
    start, end = _ops_log_window(hours=hours)
    query = build_logs_query(service=service, level=level, text_query=text_query)

    try:
        payload = await fetch_json(
            _join_url(
                loki_url,
                "/loki/api/v1/query_range",
                {
                    "query": query,
                    "start": int(start.timestamp() * 1_000_000_000),
                    "end": int(end.timestamp() * 1_000_000_000),
                    "limit": max(limit, 1),
                    "direction": "backward",
                },
            )
        )
        raw_streams = payload.get("data", {}).get("result", [])
        items: list[dict[str, Any]] = []
        for stream in raw_streams:
            labels = stream.get("stream", {})
            for timestamp_ns, line in stream.get("values", []):
                try:
                    timestamp = datetime.fromtimestamp(
                        int(timestamp_ns) / 1_000_000_000,
                        tz=timezone.utc,
                    )
                except (TypeError, ValueError, OSError):
                    timestamp = end
                items.append(
                    {
                        "timestamp": serialize_datetime(timestamp),
                        "service": str(labels.get("service") or labels.get("container") or "unknown"),
                        "source": str(labels.get("source") or "unknown"),
                        "level": str(labels.get("level") or "unknown"),
                        "message": str(line).rstrip(),
                    }
                )
        items.sort(key=lambda item: item["timestamp"], reverse=True)
        return {
            "ok": True,
            "generated_at": serialize_datetime(end),
            "query": query,
            "items": items[:limit],
        }
    except Exception as exc:  # pragma: no cover - operational path
        return {
            "ok": False,
            "generated_at": serialize_datetime(end),
            "query": query,
            "items": [],
            "error": str(exc),
        }
