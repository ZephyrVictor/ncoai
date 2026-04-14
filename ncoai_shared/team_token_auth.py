from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from redis.asyncio import Redis


TEAM_TOKEN_AUTH_CONFIG_KEY = "global"
TEAM_TOKEN_AUTH_CONFIG_REDIS_KEY = "team-token-auth:config:global"
TEAM_TOKEN_AUTH_EVENTS_CHANNEL = "team-token-auth:events"
TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED = "public_key_updated"
TEAM_TOKEN_AUTH_VERSION_METADATA_KEY = "team_token_auth_version"


def serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


@dataclass(slots=True)
class CachedTeamTokenAuthConfig:
    config_key: str
    public_key: str
    config_version: int
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_key": self.config_key,
            "public_key": self.public_key,
            "config_version": self.config_version,
            "updated_at": self.updated_at,
        }

    def to_redis_mapping(self) -> dict[str, str | int]:
        return {
            "config_key": self.config_key,
            "public_key": self.public_key,
            "config_version": self.config_version,
            "updated_at": self.updated_at or "",
        }


def config_from_mapping(
    config: Mapping[str, Any] | CachedTeamTokenAuthConfig | None,
) -> CachedTeamTokenAuthConfig | None:
    if config is None:
        return None
    if isinstance(config, CachedTeamTokenAuthConfig):
        return config
    if not config:
        return None
    public_key = str(config.get("public_key") or "").strip()
    if not public_key:
        return None
    return CachedTeamTokenAuthConfig(
        config_key=str(config.get("config_key") or TEAM_TOKEN_AUTH_CONFIG_KEY),
        public_key=public_key,
        config_version=int(config.get("config_version") or 0),
        updated_at=serialize_datetime(config.get("updated_at")),
    )


async def load_cached_team_token_auth_config(
    redis: Redis,
) -> CachedTeamTokenAuthConfig | None:
    values = await redis.hgetall(TEAM_TOKEN_AUTH_CONFIG_REDIS_KEY)
    if not values:
        return None
    return config_from_mapping(values)


async def store_cached_team_token_auth_config(
    redis: Redis,
    config: Mapping[str, Any] | CachedTeamTokenAuthConfig,
) -> CachedTeamTokenAuthConfig:
    cached_config = config_from_mapping(config)
    if cached_config is None:
        raise ValueError("config is required")
    await redis.hset(
        TEAM_TOKEN_AUTH_CONFIG_REDIS_KEY,
        mapping=cached_config.to_redis_mapping(),
    )
    return cached_config


async def clear_cached_team_token_auth_config(redis: Redis) -> None:
    await redis.delete(TEAM_TOKEN_AUTH_CONFIG_REDIS_KEY)


def build_team_token_auth_event(
    config: Mapping[str, Any] | CachedTeamTokenAuthConfig,
    *,
    event_type: str = TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED,
) -> str:
    cached_config = config_from_mapping(config)
    if cached_config is None:
        raise ValueError("config is required")
    return json.dumps(
        {
            "event": event_type,
            "config_key": cached_config.config_key,
            "config_version": cached_config.config_version,
            "updated_at": cached_config.updated_at,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_team_token_auth_event(payload: str | bytes | bytearray) -> dict[str, Any]:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("event payload must be an object")
    return {
        "event": str(data.get("event") or ""),
        "config_key": str(data.get("config_key") or TEAM_TOKEN_AUTH_CONFIG_KEY),
        "config_version": int(data.get("config_version") or 0),
        "updated_at": serialize_datetime(data.get("updated_at")),
    }
