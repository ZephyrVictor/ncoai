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
    "ncoai_admin_app_for_tests",
    APP_DIR / "app.py",
)
assert ADMIN_APP_SPEC and ADMIN_APP_SPEC.loader
admin_app = importlib.util.module_from_spec(ADMIN_APP_SPEC)
ADMIN_APP_SPEC.loader.exec_module(admin_app)
for module_name in ("settings", "ops_service", "database"):
    sys.modules.pop(module_name, None)


def sample_summary() -> dict[str, object]:
    return {
        "ok": True,
        "generated_at": "2026-04-05T00:00:00+00:00",
        "resources": {
            "cpu_percent": 12.3,
            "memory_percent": 45.6,
            "disk_percent": 67.8,
        },
        "services": [
            {"name": "Prometheus", "status": "up", "detail": "All targets are healthy."},
        ],
        "health_checks": [
            {"name": "Chainlit Health", "status": "up", "detail": "All targets are healthy."},
        ],
        "alerts": {"firing_count": 0, "pending_count": 0},
        "links": {
            "grafana": "/grafana/",
            "overview_dashboard": "/grafana/d/ops-overview/ops-overview",
            "logs_dashboard": "/grafana/d/logs-errors/logs-errors",
        },
        "errors": [],
    }


def sample_alerts() -> dict[str, object]:
    return {
        "ok": True,
        "generated_at": "2026-04-05T00:00:00+00:00",
        "items": [
            {
                "name": "RedisDown",
                "state": "firing",
                "severity": "critical",
                "summary": "Redis is down.",
                "description": "",
                "active_at": "2026-04-05T00:00:00+00:00",
            }
        ],
    }


def sample_logs() -> dict[str, object]:
    return {
        "ok": True,
        "generated_at": "2026-04-05T00:00:00+00:00",
        "query": "{job=~\".+\"}",
        "items": [
            {
                "timestamp": "2026-04-05T00:00:00+00:00",
                "service": "chainlit",
                "source": "host",
                "level": "error",
                "message": "Chat request failed",
            }
        ],
    }


def build_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        admin_app,
        "REDIS",
        SimpleNamespace(ping=AsyncMock(return_value=True), close=AsyncMock()),
    )
    monkeypatch.setattr(admin_app, "ENGINE", SimpleNamespace(dispose=AsyncMock()))
    return TestClient(admin_app.app)


def test_ops_summary_api_requires_admin(monkeypatch) -> None:
    client = build_client(monkeypatch)

    response = client.get("/admin/api/ops/summary")

    assert response.status_code == 401


def test_ops_summary_api_returns_payload_for_admin(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(admin_app, "load_ops_summary", AsyncMock(return_value=sample_summary()))

    response = client.get("/admin/api/ops/summary")

    assert response.status_code == 200
    assert response.json()["links"]["grafana"] == "/grafana/"


def test_ops_page_renders_summary_alerts_and_logs(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(admin_app, "load_ops_summary", AsyncMock(return_value=sample_summary()))
    monkeypatch.setattr(admin_app, "load_ops_alerts", AsyncMock(return_value=sample_alerts()))
    monkeypatch.setattr(admin_app, "load_ops_logs", AsyncMock(return_value=sample_logs()))

    response = client.get("/admin/ops")
    body = response.text

    assert response.status_code == 200
    assert "运维总览" in body
    assert "RedisDown" in body
    assert "Chat request failed" in body


def test_admin_metrics_endpoint_exposes_prometheus_format(monkeypatch) -> None:
    client = build_client(monkeypatch)

    response = client.get("/admin/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "ncoai_http_requests_total" in response.text
