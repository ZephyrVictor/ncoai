from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ncoai_shared.controlplane import ControlPlaneRepository
from ncoai_shared.gzctf_team_token import resolve_public_key_base64
from ncoai_shared.quota_cache import (
    clear_cached_global_quota_policy,
    store_cached_global_quota_policy,
)
from ncoai_shared.schema import CHAINLIT_SCHEMA_STATEMENTS
from ncoai_shared.team_token_auth import (
    clear_cached_team_token_auth_config,
    store_cached_team_token_auth_config,
)

DEFAULT_GLOBAL_WINDOW_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
DEFAULT_GLOBAL_WINDOW_END = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def parse_iso_datetime(
    value: str | None,
    *,
    default: datetime | None = None,
) -> datetime | None:
    if not value:
        return default
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def ensure_application_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for statement in CHAINLIT_SCHEMA_STATEMENTS:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def bootstrap_control_plane(
    engine: AsyncEngine,
    *,
    admin_username: str,
    admin_password_hash: str,
    total_token_limit: int,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any] | None:
    repository = ControlPlaneRepository(engine)
    await repository.bootstrap_admin_account(
        username=admin_username,
        password_hash=admin_password_hash,
    )
    await repository.bootstrap_global_quota_policy(
        total_token_limit=total_token_limit,
        window_start=window_start or DEFAULT_GLOBAL_WINDOW_START,
        window_end=window_end or DEFAULT_GLOBAL_WINDOW_END,
    )
    return await repository.get_global_quota_policy()


async def sync_global_quota_policy_cache(
    engine: AsyncEngine,
    redis: Redis,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    repository = ControlPlaneRepository(engine)
    active_policy = policy or await repository.get_global_quota_policy()
    if active_policy is None:
        await clear_cached_global_quota_policy(redis)
        return None
    await store_cached_global_quota_policy(redis, active_policy)
    return active_policy


async def bootstrap_team_token_auth_config(
    engine: AsyncEngine,
    *,
    public_key_base64: str | None,
    private_key_base64: str | None,
) -> dict[str, Any] | None:
    repository = ControlPlaneRepository(engine)
    existing = await repository.get_team_token_auth_config()
    if existing is not None:
        return existing

    public_key = (public_key_base64 or "").strip()
    private_key = (private_key_base64 or "").strip()
    if not public_key and not private_key:
        return None

    resolved_public_key = resolve_public_key_base64(public_key, private_key)
    return await repository.bootstrap_team_token_auth_config(resolved_public_key)


async def sync_team_token_auth_config_cache(
    engine: AsyncEngine,
    redis: Redis,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    repository = ControlPlaneRepository(engine)
    active_config = config or await repository.get_team_token_auth_config()
    if active_config is None:
        await clear_cached_team_token_auth_config(redis)
        return None
    await store_cached_team_token_auth_config(redis, active_config)
    return active_config
