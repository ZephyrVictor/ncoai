from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from redis.asyncio import Redis

GLOBAL_QUOTA_CONFIG_KEY = "quota:config:global"


def serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class CachedGlobalQuotaPolicy:
    policy_id: str | None
    total_token_limit: int
    window_start: str | None
    window_end: str | None
    is_active: bool
    policy_version: int
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.policy_id,
            "scope_type": "global",
            "scope_id": None,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_token_limit": self.total_token_limit,
            "is_active": self.is_active,
            "policy_version": self.policy_version,
            "updated_at": self.updated_at,
        }

    def to_redis_mapping(self) -> dict[str, str | int]:
        return {
            "policy_id": self.policy_id or "",
            "total_limit_tokens": self.total_token_limit,
            "window_start": self.window_start or "",
            "window_end": self.window_end or "",
            "is_active": "1" if self.is_active else "0",
            "policy_version": self.policy_version,
            "updated_at": self.updated_at or "",
        }


def policy_from_mapping(
    policy: Mapping[str, Any] | CachedGlobalQuotaPolicy | None,
) -> CachedGlobalQuotaPolicy | None:
    if policy is None:
        return None
    if isinstance(policy, CachedGlobalQuotaPolicy):
        return policy
    if not policy:
        return None
    return CachedGlobalQuotaPolicy(
        policy_id=str(policy.get("id") or policy.get("policy_id") or "") or None,
        total_token_limit=int(
            policy.get("total_token_limit")
            or policy.get("total_limit_tokens")
            or 0
        ),
        window_start=serialize_datetime(policy.get("window_start")),
        window_end=serialize_datetime(policy.get("window_end")),
        is_active=_parse_bool(policy.get("is_active"), default=False),
        policy_version=int(policy.get("policy_version") or 0),
        updated_at=serialize_datetime(policy.get("updated_at")),
    )


async def load_cached_global_quota_policy(
    redis: Redis,
) -> CachedGlobalQuotaPolicy | None:
    values = await redis.hgetall(GLOBAL_QUOTA_CONFIG_KEY)
    if not values:
        return None
    return policy_from_mapping(values)


async def store_cached_global_quota_policy(
    redis: Redis,
    policy: Mapping[str, Any] | CachedGlobalQuotaPolicy,
) -> CachedGlobalQuotaPolicy:
    cached_policy = policy_from_mapping(policy)
    if cached_policy is None:
        raise ValueError("policy is required")
    await redis.hset(
        GLOBAL_QUOTA_CONFIG_KEY,
        mapping=cached_policy.to_redis_mapping(),
    )
    return cached_policy


async def clear_cached_global_quota_policy(redis: Redis) -> None:
    await redis.delete(GLOBAL_QUOTA_CONFIG_KEY)
