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
    "ncoai_admin_team_token_auth_app_for_tests",
    APP_DIR / "app.py",
)
assert ADMIN_APP_SPEC and ADMIN_APP_SPEC.loader
admin_app = importlib.util.module_from_spec(ADMIN_APP_SPEC)
ADMIN_APP_SPEC.loader.exec_module(admin_app)
for module_name in ("settings", "ops_service", "database"):
    sys.modules.pop(module_name, None)


def build_client(monkeypatch) -> TestClient:
    redis = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        close=AsyncMock(),
        publish=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(admin_app, "REDIS", redis)
    monkeypatch.setattr(admin_app, "ENGINE", SimpleNamespace(dispose=AsyncMock()))
    return TestClient(admin_app.app)


def test_get_team_token_auth_api_requires_admin(monkeypatch) -> None:
    client = build_client(monkeypatch)

    response = client.get("/admin/api/team-token-auth")

    assert response.status_code == 401


def test_get_team_token_auth_api_returns_config(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app,
        "get_team_token_auth_config",
        AsyncMock(
            return_value={
                "config_key": "global",
                "public_key": "test-public-key",
                "config_version": 3,
                "updated_at": "2026-04-08T00:00:00+00:00",
            }
        ),
    )

    response = client.get("/admin/api/team-token-auth")

    assert response.status_code == 200
    assert response.json()["item"]["config_version"] == 3


def test_update_team_token_auth_api_publishes_only_when_changed(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app,
        "REPOSITORY",
        SimpleNamespace(
            upsert_team_token_auth_config=AsyncMock(
                return_value=(
                    {
                        "config_key": "global",
                        "public_key": "sYbpAtriKD1DEmoAzp1vorouIpiPpCoBeW9RN4rhpBQ=",
                        "config_version": 2,
                        "updated_at": "2026-04-08T00:00:00+00:00",
                    },
                    True,
                )
            )
        ),
    )
    monkeypatch.setattr(
        admin_app,
        "sync_team_token_auth_config_cache",
        AsyncMock(),
    )
    monkeypatch.setattr(
        admin_app,
        "publish_team_token_auth_config_update",
        AsyncMock(),
    )

    response = client.put(
        "/admin/api/team-token-auth",
        json={"public_key": "sYbpAtriKD1DEmoAzp1vorouIpiPpCoBeW9RN4rhpBQ="},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is True
    admin_app.sync_team_token_auth_config_cache.assert_awaited_once()
    admin_app.publish_team_token_auth_config_update.assert_awaited_once()


def test_team_token_auth_page_renders_config_summary(monkeypatch) -> None:
    client = build_client(monkeypatch)
    monkeypatch.setattr(admin_app, "admin_authenticated", lambda request: True)
    monkeypatch.setattr(
        admin_app,
        "get_team_token_auth_config",
        AsyncMock(
            return_value={
                "config_key": "global",
                "public_key": "test-public-key",
                "config_version": 5,
                "updated_at": "2026-04-08T00:00:00+00:00",
            }
        ),
    )

    response = client.get("/admin/team-token-auth")

    assert response.status_code == 200
    assert "Team Token 公钥配置" in response.text
    assert "配置版本" in response.text
