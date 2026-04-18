from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from monitoring_notifications import (  # noqa: E402
    build_secret_cipher,
    normalize_notification_channel_payload,
)

ADMIN_APP_SPEC = importlib.util.spec_from_file_location(
    "ncoai_admin_monitoring_notifications_app_for_tests",
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


def sample_channel() -> dict[str, object]:
    return {
        "id": "channel-1",
        "name": "Ops SMTP",
        "channel_type": "smtp_email",
        "is_active": True,
        "config_public": {
            "host": "smtp.example.com",
            "port": 587,
            "username": "ops",
            "from_email": "ops@example.com",
            "to_emails": ["team@example.com"],
            "security": "starttls",
        },
        "has_secret": True,
        "last_test_at": None,
    }


class FakeController:
    def __init__(self) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()


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


def test_secret_cipher_round_trip() -> None:
    key = Fernet.generate_key().decode("utf-8")
    cipher = build_secret_cipher(key)
    encrypted = cipher.encrypt_mapping({"smtp_password": "secret"})
    assert encrypted
    assert cipher.decrypt_mapping(encrypted)["smtp_password"] == "secret"


def test_normalize_notification_payload_parses_smtp_fields() -> None:
    payload = normalize_notification_channel_payload(
        name="Ops Mail",
        channel_type="smtp_email",
        is_active=True,
        config_public={
            "host": "smtp.example.com",
            "port": 587,
            "username": "ops",
            "from_email": "ops@example.com",
            "to_emails": "a@example.com,b@example.com",
            "security": "starttls",
        },
        config_secret={"smtp_password": "secret"},
    )
    assert payload["config_public"]["to_emails"] == ["a@example.com", "b@example.com"]


def test_monitoring_channels_list_api_returns_sanitized_payload(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app.REPOSITORY,
        "list_monitor_notification_channels",
        AsyncMock(return_value=[sample_channel()]),
    )

    response = client.get("/admin/api/monitoring/channels")

    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Ops SMTP"
    assert "config_secret_ciphertext" not in response.json()["items"][0]


def test_monitoring_channel_test_api_uses_sender(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app.REPOSITORY,
        "get_monitor_notification_channel",
        AsyncMock(return_value={**sample_channel(), "config_secret_ciphertext": "cipher"}),
    )
    monkeypatch.setattr(
        admin_app.REPOSITORY,
        "touch_monitor_notification_channel_test",
        AsyncMock(),
    )
    monkeypatch.setattr(
        admin_app,
        "send_test_notification",
        AsyncMock(return_value={"ok": True, "detail": "sent"}),
    )

    response = client.post("/admin/api/monitoring/channels/channel-1/test")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    admin_app.send_test_notification.assert_awaited_once()
