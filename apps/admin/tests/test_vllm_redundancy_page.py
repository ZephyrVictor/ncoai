from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

ADMIN_APP_SPEC = importlib.util.spec_from_file_location(
    "ncoai_admin_vllm_redundancy_app_for_tests",
    APP_DIR / "app.py",
)
assert ADMIN_APP_SPEC and ADMIN_APP_SPEC.loader
admin_app = importlib.util.module_from_spec(ADMIN_APP_SPEC)
ADMIN_APP_SPEC.loader.exec_module(admin_app)
for module_name in ("settings", "ops_service", "database", "vllm_redundancy"):
    sys.modules.pop(module_name, None)


def sample_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-04-08T00:00:00+00:00",
        "mode": "failed_over",
        "config_error": "",
        "config": {
            "failover_enabled": True,
            "primary_origin": "http://127.0.0.1:12233",
            "primary_api_base": "http://127.0.0.1:12233/v1",
            "backup_origin": "http://backup.example.com:12233",
            "backup_api_base": "http://backup.example.com:12233/v1",
            "primary_request_timeout_seconds": 2.5,
            "backup_request_timeout_seconds": 5.0,
        },
        "primary": {
            "healthy": False,
            "process_running": False,
            "managed": True,
            "pid": None,
            "health_url": "http://127.0.0.1:12233/health",
            "models_url": "http://127.0.0.1:12233/v1/models",
            "last_error": "connection refused",
        },
        "backup": {
            "configured": True,
            "healthy": True,
            "health_url": "http://backup.example.com:12233/health",
            "models_url": "http://backup.example.com:12233/v1/models",
            "last_error": "",
        },
        "last_restart": {
            "status": "success",
            "attempted_at": "2026-04-08T00:00:00+00:00",
            "completed_at": "2026-04-08T00:01:00+00:00",
            "detail": "ok",
            "reason": "primary unhealthy",
        },
        "restart_budget": {
            "attempts_in_window": 1,
            "max_attempts": 3,
            "exhausted": False,
            "next_allowed_at": "2026-04-08T00:03:00+00:00",
        },
        "events": [
            {
                "timestamp": "2026-04-08T00:00:00+00:00",
                "level": "warning",
                "event": "failover_activated",
                "message": "Local vLLM is unhealthy.",
            }
        ],
    }


class FakeController:
    def __init__(self) -> None:
        self.load_payload = AsyncMock(return_value=sample_payload())
        self.refresh_now = AsyncMock(return_value=sample_payload())
        self.start = AsyncMock()
        self.stop = AsyncMock()

    def monitoring_enabled(self) -> bool:
        return False


def build_client(monkeypatch, controller: FakeController) -> TestClient:
    monkeypatch.setattr(
        admin_app,
        "REDIS",
        SimpleNamespace(ping=AsyncMock(return_value=True), close=AsyncMock()),
    )
    monkeypatch.setattr(admin_app, "ENGINE", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(admin_app, "get_vllm_redundancy_controller", lambda: controller)
    return TestClient(admin_app.app)


def test_vllm_redundancy_page_renders_status(monkeypatch) -> None:
    controller = FakeController()
    client = build_client(monkeypatch, controller)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)

    response = client.get("/admin/vllm-redundancy")

    assert response.status_code == 200
    assert "vLLM 冗余状态" in response.text
    assert "http://backup.example.com:12233" in response.text
    controller.load_payload.assert_awaited_once()


def test_vllm_redundancy_ping_api_returns_latest_snapshot(monkeypatch) -> None:
    controller = FakeController()
    client = build_client(monkeypatch, controller)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)

    response = client.post("/admin/api/vllm-redundancy/ping")

    assert response.status_code == 200
    assert response.json()["mode"] == "failed_over"
    controller.refresh_now.assert_awaited_once()
