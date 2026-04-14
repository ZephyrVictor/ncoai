from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ncoai_shared.bootstrap as bootstrap_module
from ncoai_shared.team_token_auth import (
    TEAM_TOKEN_AUTH_CONFIG_KEY,
    TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED,
    build_team_token_auth_event,
    load_cached_team_token_auth_config,
    parse_team_token_auth_event,
    store_cached_team_token_auth_config,
)


def generate_key_material() -> dict[str, str]:
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
    return {
        "private_key": base64.b64encode(private_key_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_key_bytes).decode("ascii"),
    }


class FakeRedis:
    def __init__(self) -> None:
        self.storage: dict[str, dict[str, str | int]] = {}

    async def hset(self, key: str, mapping: dict[str, str | int]) -> None:
        self.storage[key] = dict(mapping)

    async def hgetall(self, key: str) -> dict[str, str | int]:
        return dict(self.storage.get(key, {}))

    async def delete(self, key: str) -> None:
        self.storage.pop(key, None)


@pytest.mark.asyncio
async def test_bootstrap_team_token_auth_config_uses_public_key(monkeypatch) -> None:
    key_material = generate_key_material()
    repository = AsyncMock()
    repository.get_team_token_auth_config = AsyncMock(return_value=None)
    repository.bootstrap_team_token_auth_config = AsyncMock(
        return_value={
            "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
            "public_key": key_material["public_key"],
            "config_version": 1,
            "updated_at": "2026-04-08T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        bootstrap_module,
        "ControlPlaneRepository",
        lambda engine: repository,
    )

    config = await bootstrap_module.bootstrap_team_token_auth_config(
        object(),
        public_key_base64=key_material["public_key"],
        private_key_base64="",
    )

    assert config is not None
    repository.bootstrap_team_token_auth_config.assert_awaited_once_with(
        key_material["public_key"]
    )


@pytest.mark.asyncio
async def test_bootstrap_team_token_auth_config_derives_public_key_from_private_key(
    monkeypatch,
) -> None:
    key_material = generate_key_material()
    repository = AsyncMock()
    repository.get_team_token_auth_config = AsyncMock(return_value=None)
    repository.bootstrap_team_token_auth_config = AsyncMock(
        return_value={
            "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
            "public_key": key_material["public_key"],
            "config_version": 1,
            "updated_at": "2026-04-08T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        bootstrap_module,
        "ControlPlaneRepository",
        lambda engine: repository,
    )

    config = await bootstrap_module.bootstrap_team_token_auth_config(
        object(),
        public_key_base64="",
        private_key_base64=key_material["private_key"],
    )

    assert config is not None
    repository.bootstrap_team_token_auth_config.assert_awaited_once_with(
        key_material["public_key"]
    )


@pytest.mark.asyncio
async def test_team_token_auth_cache_and_event_roundtrip() -> None:
    redis = FakeRedis()
    config = {
        "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
        "public_key": generate_key_material()["public_key"],
        "config_version": 7,
        "updated_at": "2026-04-08T00:00:00+00:00",
    }

    await store_cached_team_token_auth_config(redis, config)
    cached = await load_cached_team_token_auth_config(redis)
    event = build_team_token_auth_event(config)
    parsed_event = parse_team_token_auth_event(event)

    assert cached is not None
    assert cached.public_key == config["public_key"]
    assert cached.config_version == 7
    assert parsed_event["event"] == TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED
    assert parsed_event["config_key"] == TEAM_TOKEN_AUTH_CONFIG_KEY
    assert parsed_event["config_version"] == 7
