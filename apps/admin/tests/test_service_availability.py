from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import service_availability as module
from settings import AdminSettings, load_settings


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
        self.values: dict[str, str] = {}
        self.expires: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        self.expires[key] = ex


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

    asyncio.run(controller.refresh_now(reason="test-1"))
    asyncio.run(controller.refresh_now(reason="test-2"))
    asyncio.run(controller.refresh_now(reason="test-3"))
    asyncio.run(controller.refresh_now(reason="test-4"))

    assert [event["event_type"] for event in repository.events] == ["down", "recovered"]


def model_controller(settings: AdminSettings, redis: FakeRedis) -> module.ServiceAvailabilityController:
    return module.ServiceAvailabilityController(
        settings=settings,
        prometheus_url="http://127.0.0.1:9090",
        repository=FakeRepository(),
        redis=redis,
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        secret_cipher=SimpleNamespace(),
    )


def test_model_checks_probe_both_aliases_and_cache_results(monkeypatch) -> None:
    settings = replace(sample_settings(), litellm_api_key="secret")
    redis = FakeRedis()
    called: list[tuple[str, str]] = []

    def fake_request(url: str, *, api_key: str, model: str) -> None:
        assert api_key == "secret"
        called.append((url, model))

    monkeypatch.setattr(module, "_request_model_completion", fake_request)
    controller = model_controller(settings, redis)
    checks = asyncio.run(controller.refresh_model_checks_now())

    assert {check["status"] for check in checks} == {"up"}
    assert {model for _, model in called} == {"qwen35b_no_think", "qwen35b_think"}
    assert all(url.endswith("/v1/chat/completions") for url, _ in called)
    assert redis.expires[module.MODEL_STATE_REDIS_KEY] == 360
    assert asyncio.run(controller.load_model_checks()) == checks


def test_model_probe_failure_is_down_and_thinking_can_be_disabled(monkeypatch) -> None:
    settings = replace(sample_settings(), litellm_api_key="secret", thinking_enabled=False)
    redis = FakeRedis()

    def fake_request(url: str, *, api_key: str, model: str) -> None:
        raise RuntimeError("LiteLLM 返回 HTTP 404")

    monkeypatch.setattr(module, "_request_model_completion", fake_request)
    checks = asyncio.run(model_controller(settings, redis).refresh_model_checks_now())

    assert len(checks) == 1
    assert checks[0]["status"] == "down"
    assert checks[0]["detail"] == "LiteLLM 返回 HTTP 404"


def test_model_check_cache_is_unknown_when_missing_or_stale() -> None:
    settings = replace(sample_settings(), litellm_api_key="secret")
    redis = FakeRedis()
    controller = model_controller(settings, redis)
    assert all(item["status"] == "unknown" for item in asyncio.run(controller.load_model_checks()))

    stale = module.serialize_datetime(module.utcnow() - timedelta(minutes=7))
    redis.values[module.MODEL_STATE_REDIS_KEY] = json.dumps({
        "model-default": {"model": "qwen35b_no_think", "status": "up", "last_checked_at": stale, "detail": "old"},
        "model-thinking": {"model": "qwen35b_think", "status": "down", "last_checked_at": stale, "detail": "old"},
    })
    assert all(item["status"] == "unknown" for item in asyncio.run(controller.load_model_checks()))


def test_model_check_cache_does_not_reuse_old_alias() -> None:
    settings = replace(sample_settings(), litellm_api_key="secret", thinking_enabled=False)
    redis = FakeRedis()
    redis.values[module.MODEL_STATE_REDIS_KEY] = json.dumps({
        "model-default": {
            "model": "previous-alias",
            "status": "up",
            "last_checked_at": module.serialize_datetime(module.utcnow()),
            "detail": "生成请求成功",
        }
    })
    checks = asyncio.run(model_controller(settings, redis).load_model_checks())
    assert checks[0]["status"] == "unknown"


def test_model_probe_without_key_does_not_send_request(monkeypatch) -> None:
    def fail_request(*args, **kwargs) -> None:
        raise AssertionError("probe must not send a request without a key")

    monkeypatch.setattr(module, "_request_model_completion", fail_request)
    check = asyncio.run(module.probe_model(
        settings=sample_settings(), check_id="model-default", name="普通模式", model="model"
    ))
    assert check["status"] == "unknown"


@pytest.mark.parametrize("message", [
    {"content": "你好"},
    {"content": None, "reasoning_content": "Let me think"},
])
def test_completion_accepts_answer_or_reasoning(monkeypatch, message) -> None:
    def fake_open(request, *, timeout):
        payload = json.loads(request.data)
        assert request.get_header("Authorization") == "Bearer secret"
        assert payload["model"] == "configured-alias"
        assert payload["max_tokens"] == 64
        assert payload["stream"] is False
        assert timeout == 60
        return io.BytesIO(json.dumps({
            "choices": [{"message": message, "finish_reason": "length"}]
        }).encode())

    monkeypatch.setattr(module, "urlopen", fake_open)
    module._request_model_completion(
        "http://local/v1/chat/completions", api_key="secret", model="configured-alias"
    )


@pytest.mark.parametrize("payload", [
    {}, {"choices": []}, {"choices": [None]},
    {"choices": [{"message": {"content": "  "}}]},
    {"choices": [{"message": {"content": None, "reasoning_content": ""}}]},
    {"error": {"message": "private error details"}},
])
def test_completion_rejects_missing_or_empty_output(monkeypatch, payload) -> None:
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    with pytest.raises(RuntimeError, match="未返回生成结果|未生成有效内容"):
        module._request_model_completion("http://local/v1/chat/completions", api_key="secret", model="alias")


@pytest.mark.parametrize("error", [
    TimeoutError("private timeout details"),
    URLError("private connection details"),
    HTTPError("http://local", 404, "private error", {}, io.BytesIO(b"private body")),
])
def test_completion_errors_do_not_expose_raw_response(monkeypatch, error) -> None:
    def fake_open(*args, **kwargs):
        raise error

    monkeypatch.setattr(module, "urlopen", fake_open)
    with pytest.raises(RuntimeError) as caught:
        module._request_model_completion("http://local/v1/chat/completions", api_key="secret", model="alias")
    assert "private" not in str(caught.value)


def test_cache_read_failure_returns_unknown() -> None:
    redis = FakeRedis()
    redis.get = AsyncMock(side_effect=ConnectionError("offline"))
    controller = model_controller(replace(sample_settings(), litellm_api_key="secret"), redis)
    assert all(item["status"] == "unknown" for item in asyncio.run(controller.load_model_checks()))


def test_monitor_tasks_start_once_and_stop_together() -> None:
    controller = model_controller(sample_settings(), FakeRedis())
    controller.refresh_now = AsyncMock()
    controller.refresh_model_checks_now = AsyncMock()

    async def run():
        await controller.start()
        tasks = (controller._task, controller._model_task)
        await controller.start()
        assert tasks == (controller._task, controller._model_task)
        await asyncio.sleep(0)
        controller.refresh_now.assert_awaited_once()
        controller.refresh_model_checks_now.assert_awaited_once()
        await controller.stop()
        assert all(task.done() for task in tasks)
        assert controller._task is None and controller._model_task is None

    asyncio.run(run())


def test_admin_model_settings_follow_existing_environment(monkeypatch) -> None:
    monkeypatch.setenv("VLLM_FAILOVER_ENABLED", "0")
    monkeypatch.setenv("CHAINLIT_DEFAULT_MODEL", "custom-chat")
    monkeypatch.setenv("CHAINLIT_THINKING_MODEL", "custom-think")
    monkeypatch.setenv("CHAINLIT_ENABLE_THINKING", "0")
    monkeypatch.setenv("CHAINLIT_LITELLM_BASE_URL", "http://gateway:4000/v1/")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "fallback-secret")
    monkeypatch.delenv("CHAINLIT_LITELLM_API_KEY", raising=False)
    settings = load_settings()
    assert settings.default_model_alias == "custom-chat"
    assert settings.thinking_model_alias == "custom-think"
    assert settings.thinking_enabled is False
    assert settings.litellm_base_url == "http://gateway:4000/v1"
    assert settings.litellm_api_key == "fallback-secret"
    monkeypatch.setenv("CHAINLIT_LITELLM_API_KEY", "chat-secret")
    assert load_settings().litellm_api_key == "chat-secret"
