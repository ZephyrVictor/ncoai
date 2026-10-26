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
    "ncoai_admin_services_app_for_tests",
    APP_DIR / "app.py",
)
assert ADMIN_APP_SPEC and ADMIN_APP_SPEC.loader
admin_app = importlib.util.module_from_spec(ADMIN_APP_SPEC)
ADMIN_APP_SPEC.loader.exec_module(admin_app)
for module_name in (
    "settings",
    "ops_service",
    "database",
    "vllm_redundancy",
    "service_availability",
    "monitoring_notifications",
):
    sys.modules.pop(module_name, None)


def sample_services_payload() -> dict[str, object]:
    return {
        "ok": True,
        "generated_at": "2026-04-18T00:00:00+00:00",
        "window_hours": 24,
        "bucket_minutes": 10,
        "poll_interval_seconds": 60,
        "errors": [],
        "model_checks": [
            {
                "id": "model-default",
                "name": "模型普通模式",
                "status": "up",
                "detail": "生成请求成功",
                "last_checked_at": "2026-04-18T00:00:00+00:00",
            }
        ],
        "services": [
            {
                "id": "admin",
                "name": "Admin",
                "status": "up",
                "availability_percent_24h": 100.0,
                "last_checked_at": "2026-04-18T00:00:00+00:00",
                "detail": "Healthy on the latest probe.",
                "history_buckets": [
                    {
                        "start_at": "2026-04-17T23:50:00+00:00",
                        "end_at": "2026-04-18T00:00:00+00:00",
                        "availability_ratio": 1.0,
                        "status": "up",
                    }
                ]
                * 144,
            }
        ],
    }


class FakeController:
    def __init__(self) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.load_model_checks = AsyncMock(return_value=sample_services_payload()["model_checks"])


def build_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        admin_app,
        "REDIS",
        SimpleNamespace(ping=AsyncMock(return_value=True), close=AsyncMock()),
    )
    monkeypatch.setattr(admin_app, "ENGINE", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(admin_app, "get_vllm_redundancy_controller", lambda: SimpleNamespace(monitoring_enabled=lambda: False, start=AsyncMock(), stop=AsyncMock()))
    monkeypatch.setattr(admin_app, "get_service_availability_controller", lambda: FakeController())
    return TestClient(admin_app.app)


def test_services_page_renders_availability(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app,
        "load_services_availability",
        AsyncMock(return_value=sample_services_payload()),
    )

    response = client.get("/admin/services")

    assert response.status_code == 200
    assert "巡检" in response.text
    assert "模型普通模式" in response.text
    assert "Admin" in response.text


def test_services_availability_api_returns_payload(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app,
        "load_services_availability",
        AsyncMock(return_value=sample_services_payload()),
    )
    controller = FakeController()
    controller.refresh_model_checks_now = AsyncMock()
    monkeypatch.setattr(admin_app, "get_service_availability_controller", lambda: controller)

    response = client.get("/admin/api/services/availability")

    assert response.status_code == 200
    assert response.json()["bucket_minutes"] == 10
    assert response.json()["poll_interval_seconds"] == 60
    assert response.json()["model_checks"][0]["status"] == "up"
    controller.load_model_checks.assert_awaited_once()
    controller.refresh_model_checks_now.assert_not_awaited()


def test_services_page_and_api_require_admin(monkeypatch) -> None:
    client = build_client(monkeypatch)
    assert client.get("/admin/services", follow_redirects=False).status_code in {302, 303, 307}
    assert client.get("/admin/api/services/availability").status_code == 401
