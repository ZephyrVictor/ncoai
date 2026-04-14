from __future__ import annotations

import asyncio
import base64
import json
import sys
from dataclasses import replace
from urllib.parse import urlencode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Request

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as chainlit_app  # noqa: E402
from gzctf_team_token import (  # noqa: E402
    parse_team_token,
    resolve_public_key_base64,
    team_token_message,
    verify_gzctf_team_token,
)


def generate_key_material(team_id: int = 7) -> dict[str, str | int]:
    private_key = Ed25519PrivateKey.generate()
    private_key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature_bytes = private_key.sign(team_token_message(team_id))

    return {
        "team_id": team_id,
        "private_key": base64.b64encode(private_key_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_key_bytes).decode("ascii"),
        "signature": base64.b64encode(signature_bytes).decode("ascii"),
    }


def build_json_request(payload: object) -> Request:
    body = json.dumps(payload).encode("utf-8")
    consumed = False

    async def receive() -> dict[str, object]:
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}

        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/team-token/login",
            "raw_path": b"/api/team-token/login",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )


def build_get_request(path: str, query: dict[str, str] | None = None) -> Request:
    query_string = urlencode(query or {}).encode("utf-8")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )


@pytest.mark.parametrize(
    ("value", "error_message"),
    [
        ("", "team token is required"),
        ("7", "team token must contain ':'"),
        ("abc:signature", "team token team id must be an integer"),
        ("0:signature", "team token team id must be positive"),
        ("7:   ", "team token signature is required"),
    ],
)
def test_parse_team_token_rejects_invalid_values(
    value: str,
    error_message: str,
) -> None:
    with pytest.raises(ValueError, match=error_message):
        parse_team_token(value)


def test_verify_gzctf_team_token_accepts_and_rejects_signatures() -> None:
    key_material = generate_key_material(team_id=42)

    assert verify_gzctf_team_token(
        key_material["team_id"],
        key_material["signature"],
        key_material["public_key"],
    )
    assert not verify_gzctf_team_token(
        43,
        key_material["signature"],
        key_material["public_key"],
    )


def test_resolve_public_key_base64_derives_from_private_key() -> None:
    key_material = generate_key_material()

    assert (
        resolve_public_key_base64("", key_material["private_key"])
        == key_material["public_key"]
    )


@pytest.fixture
def team_token_mode(monkeypatch):
    key_material = generate_key_material(team_id=17)
    fake_data_layer = SimpleNamespace(create_user=AsyncMock())

    monkeypatch.setenv(
        "CHAINLIT_AUTH_SECRET",
        "test-auth-secret-with-at-least-thirty-two-bytes",
    )
    monkeypatch.setattr(
        chainlit_app,
        "SETTINGS",
        replace(
            chainlit_app.SETTINGS,
            team_token_auth=replace(
                chainlit_app.SETTINGS.team_token_auth,
                mode="team_token",
                fixed_game_id=2026,
                fixed_game_public_key=key_material["public_key"],
                fixed_game_private_key="",
            ),
        ),
    )
    monkeypatch.setattr(
        chainlit_app.config.code,
        "password_auth_callback",
        chainlit_app.password_auth_callback,
        raising=False,
    )
    monkeypatch.setattr(chainlit_app, "get_data_layer", lambda: fake_data_layer)
    monkeypatch.setattr(
        chainlit_app,
        "get_team_token_auth_config",
        AsyncMock(
            return_value={
                "config_key": "global",
                "public_key": key_material["public_key"],
                "config_version": 4,
                "updated_at": "2026-04-08T00:00:00+00:00",
            }
        ),
    )
    monkeypatch.setattr(
        chainlit_app,
        "ensure_team_token_subject",
        AsyncMock(
            return_value={
                "identifier": "gzctf-team:2026:17",
                "is_active": True,
                "is_deleted": False,
            }
        ),
    )

    chainlit_app.configure_team_token_auth_mode()

    return {
        **key_material,
        "data_layer": fake_data_layer,
    }


@pytest.mark.asyncio
async def test_get_team_token_login_page_renders_single_token_form(team_token_mode) -> None:
    response = await chainlit_app.get_team_token_login_page(build_get_request("/login"))

    assert response.status_code == 200
    assert "请输入您在CTF平台中的Team token来进入对话" in response.body.decode("utf-8")
    assert "teamToken" in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_get_team_token_login_page_preserves_next_path(team_token_mode) -> None:
    response = await chainlit_app.get_team_token_login_page(
        build_get_request("/login", {"next": "/chat"})
    )

    assert response.status_code == 200
    assert '"nextPath": "/chat"' not in response.body.decode("utf-8")
    assert 'const nextPath = "/chat";' in response.body.decode("utf-8")


def test_configure_team_token_auth_mode_registers_login_routes(team_token_mode) -> None:
    route_paths = [
        route.path
        for route in chainlit_app.chainlit_app.router.routes
        if getattr(route, "path", None)
    ]

    assert chainlit_app.chainlit_route_exists("/login", "GET")
    assert chainlit_app.chainlit_route_exists("/api/team-token/login", "POST")
    assert route_paths.index("/login") < route_paths.index("/{full_path:path}")
    assert route_paths.index("/api/team-token/login") < route_paths.index(
        "/{full_path:path}"
    )


@pytest.mark.asyncio
async def test_post_team_token_login_sets_chat_cookie(team_token_mode) -> None:
    request = build_json_request(
        {"teamToken": f"{team_token_mode['team_id']}:{team_token_mode['signature']}"}
    )

    response = await chainlit_app.post_team_token_login(request)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["redirectTo"] == "/"
    assert payload["user"]["identifier"] == "gzctf-team:2026:17"
    assert "access_token=" in response.headers["set-cookie"]
    team_token_mode["data_layer"].create_user.assert_awaited_once()
    created_user = team_token_mode["data_layer"].create_user.await_args.args[0]
    assert created_user.metadata["team_token_auth_version"] == 4


@pytest.mark.asyncio
async def test_post_team_token_login_honors_safe_next_path(team_token_mode) -> None:
    request = build_json_request(
        {
            "teamToken": f"{team_token_mode['team_id']}:{team_token_mode['signature']}",
            "next": "/chat",
        }
    )

    response = await chainlit_app.post_team_token_login(request)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["redirectTo"] == "/chat"


@pytest.mark.asyncio
async def test_post_team_token_login_rejects_invalid_signature(team_token_mode) -> None:
    request = build_json_request({"teamToken": "17:ZmFrZS1zaWduYXR1cmU="})

    response = await chainlit_app.post_team_token_login(request)
    payload = json.loads(response.body)

    assert response.status_code == 401
    assert payload["detail"] == "invalid team token"


@pytest.mark.asyncio
async def test_ensure_team_token_subject_bootstraps_managed_user(monkeypatch) -> None:
    user = chainlit_app.build_team_user(2026, 17)
    repository = SimpleNamespace(
        get_managed_user=AsyncMock(
            side_effect=[
                None,
                {
                    "identifier": user.identifier,
                    "display_name": user.display_name,
                    "is_active": True,
                    "is_deleted": False,
                },
            ]
        ),
        create_managed_user=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "CONTROL_PLANE_REPOSITORY", repository)

    managed_user = await chainlit_app.ensure_team_token_subject(user)

    repository.create_managed_user.assert_awaited_once_with(
        identifier="gzctf-team:2026:17",
        display_name="Team #17",
        password_hash=chainlit_app.TEAM_TOKEN_AUTH_PASSWORD_HASH,
        is_active=True,
    )
    assert managed_user["identifier"] == "gzctf-team:2026:17"


@pytest.mark.asyncio
async def test_current_chat_subject_allowed_blocks_disabled_team_subject(
    monkeypatch,
    team_token_mode,
) -> None:
    monkeypatch.setattr(
        chainlit_app,
        "current_managed_user",
        AsyncMock(return_value={"is_active": False, "is_deleted": False}),
    )

    assert await chainlit_app.current_chat_subject_allowed() is False


@pytest.mark.asyncio
async def test_post_team_token_login_rejects_disabled_team_subject(
    monkeypatch,
    team_token_mode,
) -> None:
    monkeypatch.setattr(
        chainlit_app,
        "ensure_team_token_subject",
        AsyncMock(
            return_value={
                "identifier": "gzctf-team:2026:17",
                "is_active": False,
                "is_deleted": False,
            }
        ),
    )
    request = build_json_request(
        {"teamToken": f"{team_token_mode['team_id']}:{team_token_mode['signature']}"}
    )

    response = await chainlit_app.post_team_token_login(request)
    payload = json.loads(response.body)

    assert response.status_code == 403
    assert payload["detail"] == "current team is disabled"


@pytest.mark.asyncio
async def test_get_current_user_with_team_token_version_guard_rejects_stale_token(
    monkeypatch,
    team_token_mode,
) -> None:
    monkeypatch.setattr(
        chainlit_app,
        "get_current_user",
        AsyncMock(
            return_value=chainlit_app.User(
                identifier="gzctf-team:2026:17",
                metadata={"auth_source": "team-token"},
            )
        ),
    )
    monkeypatch.setattr(
        chainlit_app,
        "decode_jwt",
        lambda token: chainlit_app.User(
            identifier="gzctf-team:2026:17",
            metadata={"team_token_auth_version": 3},
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chainlit_app.get_current_user_with_team_token_version_guard(
            token="stale-token",
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Authentication expired. Please log in again."


@pytest.mark.asyncio
async def test_listen_for_team_token_auth_updates_emits_reload_on_newer_version(
    monkeypatch,
    team_token_mode,
) -> None:
    class FakePubSub:
        def __init__(self) -> None:
            self.calls = 0

        async def subscribe(self, channel: str) -> None:
            assert channel == "team-token-auth:events"

        async def get_message(
            self,
            *,
            ignore_subscribe_messages: bool,
            timeout: float,
        ) -> dict[str, object] | None:
            del ignore_subscribe_messages, timeout
            self.calls += 1
            if self.calls == 1:
                return {
                    "type": "message",
                    "data": json.dumps(
                        {
                            "event": "public_key_updated",
                            "config_key": "global",
                            "config_version": 6,
                            "updated_at": "2026-04-08T00:00:00+00:00",
                        }
                    ),
                }
            raise asyncio.CancelledError()

        async def close(self) -> None:
            return None

    class FakeRedisClient:
        def __init__(self) -> None:
            self.pubsub_client = FakePubSub()

        @classmethod
        def from_url(cls, url: str, *, decode_responses: bool):
            del url, decode_responses
            return cls()

        async def ping(self) -> bool:
            return True

        def pubsub(self) -> FakePubSub:
            return self.pubsub_client

        async def close(self) -> None:
            return None

    monkeypatch.setattr(chainlit_app, "Redis", FakeRedisClient)
    monkeypatch.setattr(chainlit_app, "sio", SimpleNamespace(emit=AsyncMock()))
    monkeypatch.setattr(chainlit_app, "TEAM_TOKEN_AUTH_LAST_VERSION", 4)

    with pytest.raises(asyncio.CancelledError):
        await chainlit_app.listen_for_team_token_auth_updates()

    chainlit_app.sio.emit.assert_awaited_once_with("reload", {})
