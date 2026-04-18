from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from monitoring_notifications import SecretCipher, dispatch_notification_to_channel
from ncoai_shared.controlplane import ControlPlaneRepository
from settings import AdminSettings


WINDOW_HOURS = 24
BUCKET_MINUTES = 10
POLL_INTERVAL_SECONDS = 60
BUCKET_COUNT = (WINDOW_HOURS * 60) // BUCKET_MINUTES
FAILURE_STREAK_THRESHOLD = 2
RECOVERY_STREAK_THRESHOLD = 2
STATE_REDIS_KEY = "service-availability:state:global"


@dataclass(frozen=True, slots=True)
class MonitorDefinition:
    id: str
    name: str
    current_expr: str
    history_expr: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _join_url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{base_url.rstrip('/')}{path}{suffix}"


def _request_json(url: str, *, timeout: int = 20) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}".strip()) from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    return json.loads(body)


async def fetch_json(url: str, *, timeout: int = 20) -> dict[str, Any]:
    return await asyncio.to_thread(_request_json, url, timeout=timeout)


async def query_prometheus(
    prometheus_url: str,
    expression: str,
) -> list[dict[str, Any]]:
    payload = await fetch_json(
        _join_url(prometheus_url, "/api/v1/query", {"query": expression}),
    )
    return list(payload.get("data", {}).get("result", []) or [])


async def query_prometheus_range(
    prometheus_url: str,
    expression: str,
    *,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> list[dict[str, Any]]:
    payload = await fetch_json(
        _join_url(
            prometheus_url,
            "/api/v1/query_range",
            {
                "query": expression,
                "start": str(start.timestamp()),
                "end": str(end.timestamp()),
                "step": str(step_seconds),
            },
        ),
    )
    return list(payload.get("data", {}).get("result", []) or [])


def floor_to_bucket(value: datetime) -> datetime:
    minute = (value.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    return value.replace(minute=minute, second=0, microsecond=0)


def current_status_from_value(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 0.999:
        return "up"
    if value <= 0.001:
        return "down"
    return "degraded"


def bucket_status_from_ratio(value: float | None) -> str:
    return current_status_from_value(value)


def average_result_value(results: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for item in results:
        raw_value = item.get("value")
        if not isinstance(raw_value, list) or len(raw_value) < 2:
            continue
        try:
            values.append(float(raw_value[1]))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def series_values_by_timestamp(results: list[dict[str, Any]]) -> dict[int, float]:
    grouped: dict[int, list[float]] = {}
    for item in results:
        for raw_value in list(item.get("values") or []):
            if not isinstance(raw_value, list) or len(raw_value) < 2:
                continue
            try:
                timestamp = int(float(raw_value[0]))
                value = float(raw_value[1])
            except (TypeError, ValueError):
                continue
            grouped.setdefault(timestamp, []).append(value)
    return {
        timestamp: sum(values) / len(values)
        for timestamp, values in grouped.items()
        if values
    }


def build_history_buckets(
    *,
    end_aligned: datetime,
    history_values: dict[int, float],
) -> list[dict[str, Any]]:
    first_bucket_end = end_aligned - timedelta(hours=WINDOW_HOURS) + timedelta(
        minutes=BUCKET_MINUTES
    )
    buckets: list[dict[str, Any]] = []
    for index in range(BUCKET_COUNT):
        bucket_end = first_bucket_end + timedelta(minutes=BUCKET_MINUTES * index)
        bucket_start = bucket_end - timedelta(minutes=BUCKET_MINUTES)
        value = history_values.get(int(bucket_end.timestamp()))
        buckets.append(
            {
                "start_at": serialize_datetime(bucket_start),
                "end_at": serialize_datetime(bucket_end),
                "availability_ratio": round(value, 4) if value is not None else None,
                "status": bucket_status_from_ratio(value),
            }
        )
    return buckets


def build_monitor_definitions(settings: AdminSettings) -> list[MonitorDefinition]:
    chainlit_health = (
        f'probe_success{{job="host-service-health",instance="http://127.0.0.1:'
        f'{settings.chainlit_port}{settings.chainlit_root_path}/health"}}'
    )
    admin_health = (
        f'probe_success{{job="host-service-health",instance="http://127.0.0.1:'
        f'{settings.port}{settings.root_path}/healthz"}}'
    )
    nginx_health = (
        f'probe_success{{job="host-service-health",instance="http://127.0.0.1:'
        f'{settings.nginx_port}/"}}'
    )
    litellm_health = (
        f'probe_success{{job="host-service-health",instance="http://127.0.0.1:'
        f'{settings.litellm_port}/test"}}'
    )
    vllm_health = (
        f'probe_success{{job="host-service-health",instance="http://127.0.0.1:'
        f'{settings.vllm_port}/metrics"}}'
    )
    monitors = [
        MonitorDefinition(
            "nginx",
            "Nginx",
            nginx_health,
            f"avg_over_time({nginx_health}[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "admin",
            "Admin",
            admin_health,
            f"avg_over_time({admin_health}[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "chainlit",
            "Chainlit",
            chainlit_health,
            f"avg_over_time({chainlit_health}[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "litellm",
            "LiteLLM",
            litellm_health,
            f"avg_over_time({litellm_health}[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "vllm",
            "vLLM",
            vllm_health,
            f"avg_over_time({vllm_health}[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "postgres",
            "PostgreSQL",
            "pg_up",
            f"avg_over_time(pg_up[{BUCKET_MINUTES}m])",
        ),
        MonitorDefinition(
            "redis",
            "Redis",
            "redis_up",
            f"avg_over_time(redis_up[{BUCKET_MINUTES}m])",
        ),
    ]
    if settings.caps_enabled:
        caps_health = (
            f'probe_success{{job="host-service-tcp",instance="127.0.0.1:'
            f'{settings.caps_ftp_port}"}}'
        )
        monitors.append(
            MonitorDefinition(
                "caps-ftp",
                "CAPS FTP",
                caps_health,
                f"avg_over_time({caps_health}[{BUCKET_MINUTES}m])",
            )
        )
    return monitors


def detail_for_status(status: str) -> str:
    if status == "up":
        return "Healthy on the latest probe."
    if status == "down":
        return "Current probe or exporter is failing."
    if status == "degraded":
        return "Probe is partially healthy in the current window."
    return "Prometheus returned no current datapoint."


async def load_current_service_statuses(
    *,
    prometheus_url: str,
    settings: AdminSettings,
) -> list[dict[str, Any]]:
    monitors = build_monitor_definitions(settings)
    generated_at = utcnow()
    current_results = await asyncio.gather(
        *[query_prometheus(prometheus_url, item.current_expr) for item in monitors],
        return_exceptions=True,
    )
    items: list[dict[str, Any]] = []
    for monitor, raw in zip(monitors, current_results, strict=True):
        if isinstance(raw, Exception):
            status = "unknown"
            detail = str(raw)
        else:
            value = average_result_value(raw)
            status = current_status_from_value(value)
            detail = detail_for_status(status)
        items.append(
            {
                "id": monitor.id,
                "name": monitor.name,
                "status": status,
                "detail": detail,
                "last_checked_at": serialize_datetime(generated_at),
            }
        )
    return items


async def load_services_availability(
    *,
    prometheus_url: str,
    settings: AdminSettings,
) -> dict[str, Any]:
    monitors = build_monitor_definitions(settings)
    generated_at = utcnow()
    end_aligned = floor_to_bucket(generated_at)
    start = end_aligned - timedelta(hours=WINDOW_HOURS) + timedelta(minutes=BUCKET_MINUTES)
    current_results = await asyncio.gather(
        *[query_prometheus(prometheus_url, item.current_expr) for item in monitors],
        return_exceptions=True,
    )
    history_results = await asyncio.gather(
        *[
            query_prometheus_range(
                prometheus_url,
                item.history_expr,
                start=start,
                end=end_aligned,
                step_seconds=BUCKET_MINUTES * 60,
            )
            for item in monitors
        ],
        return_exceptions=True,
    )
    services: list[dict[str, Any]] = []
    errors: list[str] = []
    for monitor, current_raw, history_raw in zip(
        monitors,
        current_results,
        history_results,
        strict=True,
    ):
        if isinstance(current_raw, Exception):
            current_value = None
            status = "unknown"
            detail = str(current_raw)
            errors.append(f"{monitor.name} current query failed: {current_raw}")
        else:
            current_value = average_result_value(current_raw)
            status = current_status_from_value(current_value)
            detail = detail_for_status(status)
        history_map: dict[int, float] = {}
        if isinstance(history_raw, Exception):
            errors.append(f"{monitor.name} history query failed: {history_raw}")
        else:
            history_map = series_values_by_timestamp(history_raw)
        history_buckets = build_history_buckets(
            end_aligned=end_aligned,
            history_values=history_map,
        )
        known_ratios = [
            float(item["availability_ratio"])
            for item in history_buckets
            if item["availability_ratio"] is not None
        ]
        availability_percent = (
            round((sum(known_ratios) / len(known_ratios)) * 100.0, 2)
            if known_ratios
            else None
        )
        services.append(
            {
                "id": monitor.id,
                "name": monitor.name,
                "status": status,
                "availability_percent_24h": availability_percent,
                "last_checked_at": serialize_datetime(generated_at),
                "detail": detail,
                "history_buckets": history_buckets,
            }
        )
    return {
        "ok": not errors,
        "generated_at": serialize_datetime(generated_at),
        "window_hours": WINDOW_HOURS,
        "bucket_minutes": BUCKET_MINUTES,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "services": services,
        "errors": errors,
    }


class ServiceAvailabilityController:
    def __init__(
        self,
        *,
        settings: AdminSettings,
        prometheus_url: str,
        repository: ControlPlaneRepository,
        redis: Any,
        logger: Any,
        secret_cipher: SecretCipher,
    ) -> None:
        self.settings = settings
        self.prometheus_url = prometheus_url
        self.repository = repository
        self.redis = redis
        self.logger = logger
        self.secret_cipher = secret_cipher
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self.refresh_now(reason="monitor")
            except Exception as exc:
                self.logger.warning("Service availability refresh failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def load_state(self) -> dict[str, Any]:
        raw = await self.redis.get(STATE_REDIS_KEY)
        if not raw:
            return {"services": {}}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"services": {}}
        return dict(parsed or {"services": {}})

    async def save_state(self, state: dict[str, Any]) -> None:
        await self.redis.set(STATE_REDIS_KEY, json.dumps(state, ensure_ascii=False))

    async def refresh_now(self, *, reason: str) -> dict[str, Any]:
        current_services = await load_current_service_statuses(
            prometheus_url=self.prometheus_url,
            settings=self.settings,
        )
        state = await self.load_state()
        service_states = dict(state.get("services") or {})
        for service in current_services:
            service_id = service["id"]
            current_status = service["status"]
            current_state = dict(service_states.get(service_id) or {})
            failing_streak = int(current_state.get("failing_streak") or 0)
            healthy_streak = int(current_state.get("healthy_streak") or 0)
            notify_state = str(current_state.get("notify_state") or "up")
            if current_status in {"down", "degraded"}:
                failing_streak += 1
                healthy_streak = 0
                if notify_state != "problem" and failing_streak >= FAILURE_STREAK_THRESHOLD:
                    await self._notify_service_event(
                        service=service,
                        event_type=current_status,
                        message=f"{service['name']} is {current_status}.",
                    )
                    notify_state = "problem"
            elif current_status == "up":
                healthy_streak += 1
                failing_streak = 0
                if notify_state == "problem" and healthy_streak >= RECOVERY_STREAK_THRESHOLD:
                    await self._notify_service_event(
                        service=service,
                        event_type="recovered",
                        message=f"{service['name']} has recovered.",
                    )
                    notify_state = "up"
            else:
                failing_streak = 0
                healthy_streak = 0
            service_states[service_id] = {
                "notify_state": notify_state,
                "failing_streak": failing_streak,
                "healthy_streak": healthy_streak,
                "last_status": current_status,
                "last_checked_at": service["last_checked_at"],
                "reason": reason,
            }
        next_state = {
            "generated_at": serialize_datetime(utcnow()),
            "services": service_states,
        }
        await self.save_state(next_state)
        return {
            "generated_at": next_state["generated_at"],
            "services": current_services,
            "reason": reason,
        }

    async def _notify_service_event(
        self,
        *,
        service: dict[str, Any],
        event_type: str,
        message: str,
    ) -> None:
        channels = [
            item
            for item in await self.repository.list_monitor_notification_channels(
                include_secret=True
            )
            if item.get("is_active")
        ]
        payload = {
            "monitor_id": service["id"],
            "monitor_name": service["name"],
            "event_type": event_type,
            "status": service["status"],
            "message": message,
            "last_checked_at": service["last_checked_at"],
        }
        subject = f"[NCOAI Monitor] {service['name']} {event_type}"
        body = (
            f"Service: {service['name']}\n"
            f"Event: {event_type}\n"
            f"Status: {service['status']}\n"
            f"Checked at: {service['last_checked_at']}\n\n"
            f"{message}"
        )
        delivery_results: dict[str, Any] = {}
        for channel in channels:
            result = await dispatch_notification_to_channel(
                channel,
                subject=subject,
                body=body,
                payload=payload,
                secret_cipher=self.secret_cipher,
            )
            delivery_results[channel["id"]] = result
        if not delivery_results:
            event_status = "skipped"
        elif all(bool(item.get("ok")) for item in delivery_results.values()):
            event_status = "sent"
        elif any(bool(item.get("ok")) for item in delivery_results.values()):
            event_status = "partial"
        else:
            event_status = "failed"
        await self.repository.create_monitor_notification_event(
            monitor_id=service["id"],
            monitor_name=service["name"],
            event_type=event_type,
            status=event_status,
            message=message,
            delivery_results=delivery_results,
        )
