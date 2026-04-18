from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import service_availability as module
from settings import AdminSettings


def sample_settings(*, caps_enabled: bool = True) -> AdminSettings:
    return AdminSettings(
        database_url="postgresql://user:pass@127.0.0.1/db",
        database_async_url="postgresql+asyncpg://user:pass@127.0.0.1/db",
        redis_url="redis://127.0.0.1:6379/0",
        host="127.0.0.1",
        port=3200,
        root_path="/admin",
        session_secret="secret",
        admin_username="admin",
        admin_password="admin",
        bootstrap_global_limit_tokens=100000,
        bootstrap_global_window_start="2024-01-01T00:00:00+00:00",
        bootstrap_global_window_end="2099-12-31T23:59:59+00:00",
        model_max_tokens=2048,
        system_prompt="hello",
        prompt_safety_tokens=64,
        tiktoken_encoding="cl100k_base",
        display_timezone="Asia/Shanghai",
        monitoring_prometheus_url="http://127.0.0.1:9090",
        monitoring_loki_url="http://127.0.0.1:3101",
        monitoring_grafana_url="/grafana/",
        monitoring_secret_key="",
        team_token_fixed_game_id=3,
        nginx_port=8088,
        chainlit_port=3100,
        chainlit_root_path="/chat",
        litellm_port=33455,
        vllm_host="127.0.0.1",
        vllm_port=12233,
        served_model_name="qwen35b",
        caps_enabled=caps_enabled,
        caps_ftp_port=3221,
        vllm_failover_enabled=False,
        vllm_backup_origin="",
        vllm_primary_request_timeout_seconds=2.5,
        vllm_backup_request_timeout_seconds=5.0,
        vllm_redundancy_probe_interval_seconds=600,
        vllm_redundancy_probe_timeout_seconds=3,
        vllm_redundancy_restart_cooldown_seconds=120,
        vllm_redundancy_restart_max_attempts=3,
        vllm_redundancy_restart_window_seconds=900,
        vllm_redundancy_restart_command_timeout_seconds=180,
    )


def test_build_monitor_definitions_includes_caps_when_enabled() -> None:
    ids = [item.id for item in module.build_monitor_definitions(sample_settings(caps_enabled=True))]
    assert "caps-ftp" in ids


def test_build_monitor_definitions_omits_caps_when_disabled() -> None:
    ids = [item.id for item in module.build_monitor_definitions(sample_settings(caps_enabled=False))]
    assert "caps-ftp" not in ids


def test_build_history_buckets_returns_fixed_24h_window() -> None:
    end = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    history_values = {}
    for index in range(module.BUCKET_COUNT):
        bucket_end = end - timedelta(hours=24) + timedelta(minutes=10 * (index + 1))
        history_values[int(bucket_end.timestamp())] = 1.0
    buckets = module.build_history_buckets(end_aligned=end, history_values=history_values)
    assert len(buckets) == 144
    assert buckets[0]["start_at"] == "2026-04-17T12:00:00+00:00"
    assert buckets[-1]["end_at"] == "2026-04-18T12:00:00+00:00"
    assert all(bucket["status"] == "up" for bucket in buckets)


async def _fake_query_prometheus(_url: str, expression: str) -> list[dict[str, object]]:
    value = 1.0
    if "redis_up" in expression:
        value = 0.0
    return [{"value": [0, str(value)]}]


async def _fake_query_range(
    _url: str,
    expression: str,
    *,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> list[dict[str, object]]:
    values = []
    current = start
    while current <= end:
        value = 1.0
        if "redis_up" in expression:
            value = 0.0
        values.append([current.timestamp(), str(value)])
        current += timedelta(seconds=step_seconds)
    return [{"values": values}]


def test_load_services_availability_aggregates_into_144_buckets(monkeypatch) -> None:
    monkeypatch.setattr(module, "query_prometheus", _fake_query_prometheus)
    monkeypatch.setattr(module, "query_prometheus_range", _fake_query_range)

    payload = __import__("asyncio").run(
        module.load_services_availability(
            prometheus_url="http://127.0.0.1:9090",
            settings=sample_settings(caps_enabled=False),
        )
    )

    assert payload["bucket_minutes"] == 10
    assert payload["poll_interval_seconds"] == 60
    assert all(len(item["history_buckets"]) == 144 for item in payload["services"])
    redis_item = next(item for item in payload["services"] if item["id"] == "redis")
    assert redis_item["status"] == "down"
    assert redis_item["availability_percent_24h"] == 0.0


class FakeRedis:
    def __init__(self) -> None:
        self.value: str | None = None

    async def get(self, _key: str) -> str | None:
        return self.value

    async def set(self, _key: str, value: str) -> None:
        self.value = value


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def list_monitor_notification_channels(self, *, include_secret: bool = False):
        return []

    async def create_monitor_notification_event(self, **kwargs):
        self.events.append(kwargs)
        return kwargs


def test_service_availability_controller_notifies_only_after_streaks(monkeypatch) -> None:
    sequence = [
        [{"id": "admin", "name": "Admin", "status": "down", "detail": "x", "last_checked_at": "2026-04-18T00:00:00+00:00"}],
        [{"id": "admin", "name": "Admin", "status": "down", "detail": "x", "last_checked_at": "2026-04-18T00:01:00+00:00"}],
        [{"id": "admin", "name": "Admin", "status": "up", "detail": "x", "last_checked_at": "2026-04-18T00:02:00+00:00"}],
        [{"id": "admin", "name": "Admin", "status": "up", "detail": "x", "last_checked_at": "2026-04-18T00:03:00+00:00"}],
    ]

    async def fake_current_statuses(*, prometheus_url: str, settings: AdminSettings):
        return sequence.pop(0)

    monkeypatch.setattr(module, "load_current_service_statuses", fake_current_statuses)

    repository = FakeRepository()
    controller = module.ServiceAvailabilityController(
        settings=sample_settings(caps_enabled=False),
        prometheus_url="http://127.0.0.1:9090",
        repository=repository,
        redis=FakeRedis(),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        secret_cipher=SimpleNamespace(),
    )

    import asyncio

    asyncio.run(controller.refresh_now(reason="test-1"))
    asyncio.run(controller.refresh_now(reason="test-2"))
    asyncio.run(controller.refresh_now(reason="test-3"))
    asyncio.run(controller.refresh_now(reason="test-4"))

    assert [event["event_type"] for event in repository.events] == ["down", "recovered"]
