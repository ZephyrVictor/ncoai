from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from ncoai_shared.monitoring import AppMetrics

MODULE_SPEC = importlib.util.spec_from_file_location(
    "ncoai_admin_vllm_redundancy_for_tests",
    APP_DIR / "vllm_redundancy.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
vllm_redundancy = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(vllm_redundancy)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = list(self.lists.get(key, []))
        if end < 0:
            return items[start:]
        return items[start : end + 1]

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = list(self.lists.get(key, []))
        self.lists[key] = items[start : end + 1]


def build_settings(**overrides):
    defaults = {
        "vllm_failover_enabled": True,
        "vllm_backup_origin": "backup.example.com:12233",
        "vllm_host": "127.0.0.1",
        "vllm_port": 12233,
        "served_model_name": "qwen35b",
        "vllm_primary_request_timeout_seconds": 2.5,
        "vllm_backup_request_timeout_seconds": 5.0,
        "vllm_redundancy_probe_interval_seconds": 15.0,
        "vllm_redundancy_probe_timeout_seconds": 0.1,
        "vllm_redundancy_restart_cooldown_seconds": 120,
        "vllm_redundancy_restart_max_attempts": 3,
        "vllm_redundancy_restart_window_seconds": 900,
        "vllm_redundancy_restart_command_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_redundancy_refresh_reports_primary_mode_when_local_node_is_healthy() -> None:
    redis = MemoryRedis()
    settings = build_settings()
    probe_results = [
        {
            "healthy": True,
            "error": "",
            "checked_at": "2026-04-08T00:00:00+00:00",
        },
        {
            "healthy": True,
            "error": "",
            "checked_at": "2026-04-08T00:00:01+00:00",
        },
    ]

    async def probe(_endpoint, _timeout):
        return probe_results.pop(0)

    controller = vllm_redundancy.VLLMRedundancyController(
        settings=settings,
        redis=redis,
        metrics=AppMetrics("admin-test"),
        logger=logging.getLogger("test"),
        probe_func=probe,
        process_reader=lambda: {
            "running": True,
            "managed": True,
            "pid": 1234,
            "source": "managed",
            "detail": "",
        },
        command_runner=lambda args, timeout: None,  # type: ignore[arg-type]
    )

    payload = asyncio.run(controller.refresh_now(reason="test", allow_restart=False))

    assert payload["mode"] == "primary"
    assert payload["primary"]["healthy"] is True
    assert payload["backup"]["healthy"] is True
    assert payload["last_restart"]["status"] == "never"


def test_redundancy_refresh_triggers_restart_and_enters_failed_over_mode() -> None:
    redis = MemoryRedis()
    settings = build_settings()
    probe_results = [
        {
            "healthy": False,
            "error": "connection refused",
            "checked_at": "2026-04-08T00:00:00+00:00",
        },
        {
            "healthy": True,
            "error": "",
            "checked_at": "2026-04-08T00:00:01+00:00",
        },
        {
            "healthy": False,
            "error": "still starting",
            "checked_at": "2026-04-08T00:00:02+00:00",
        },
    ]
    process_states = [
        {
            "running": False,
            "managed": True,
            "pid": None,
            "source": "managed",
            "detail": "No vLLM process is running.",
        },
        {
            "running": False,
            "managed": True,
            "pid": None,
            "source": "managed",
            "detail": "No vLLM process is running.",
        },
    ]
    command_calls: list[list[str]] = []

    async def probe(_endpoint, _timeout):
        return probe_results.pop(0)

    def process_reader():
        return process_states.pop(0)

    async def command_runner(args: list[str], _timeout: float):
        command_calls.append(args)
        return {
            "ok": True,
            "returncode": 0,
            "command": args,
            "output": "ok",
            "timed_out": False,
        }

    controller = vllm_redundancy.VLLMRedundancyController(
        settings=settings,
        redis=redis,
        metrics=AppMetrics("admin-test"),
        logger=logging.getLogger("test"),
        probe_func=probe,
        process_reader=process_reader,
        command_runner=command_runner,
    )

    payload = asyncio.run(controller.refresh_now(reason="monitor", allow_restart=True))

    assert command_calls == [["down", "vllm"], ["up", "vllm"]]
    assert payload["mode"] == "failed_over"
    assert payload["last_restart"]["status"] == "success"
    assert any(item["event"] == "restart_success" for item in payload["events"])
    assert any(item["event"] == "failover_activated" for item in payload["events"])


def test_redundancy_refresh_marks_budget_exhausted_without_restarting() -> None:
    redis = MemoryRedis()
    settings = build_settings(
        vllm_redundancy_restart_max_attempts=1,
        vllm_redundancy_restart_cooldown_seconds=0,
    )
    now = vllm_redundancy.utcnow()
    redis.values[vllm_redundancy.STATE_REDIS_KEY] = json.dumps(
        {
            "recent_restart_attempts": [vllm_redundancy.serialize_datetime(now)],
            "last_restart": {
                "status": "failure",
                "reason": "primary unhealthy",
                "attempted_at": vllm_redundancy.serialize_datetime(now),
                "completed_at": vllm_redundancy.serialize_datetime(now),
                "detail": "previous failure",
            },
        }
    )
    command_calls: list[list[str]] = []

    async def probe(_endpoint, _timeout):
        return {
            "healthy": False,
            "error": "down",
            "checked_at": "2026-04-08T00:00:00+00:00",
        }

    controller = vllm_redundancy.VLLMRedundancyController(
        settings=settings,
        redis=redis,
        metrics=AppMetrics("admin-test"),
        logger=logging.getLogger("test"),
        probe_func=probe,
        process_reader=lambda: {
            "running": False,
            "managed": True,
            "pid": None,
            "source": "managed",
            "detail": "No vLLM process is running.",
        },
        command_runner=lambda args, timeout: command_calls.append(args),  # type: ignore[arg-type]
    )

    payload = asyncio.run(controller.refresh_now(reason="monitor", allow_restart=True))

    assert command_calls == []
    assert payload["last_restart"]["status"] == "budget_exhausted"
    assert payload["restart_budget"]["exhausted"] is True
    assert payload["mode"] == "degraded"
