#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from psycopg import sql
import psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from redis.asyncio import Redis

from ncoai_shared.bootstrap import (
    bootstrap_control_plane,
    bootstrap_team_token_auth_config,
    ensure_application_schema,
    parse_iso_datetime,
    sync_global_quota_policy_cache,
    sync_team_token_auth_config_cache,
)
from ncoai_shared.security import hash_password


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def build_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgresql+asyncpg://"):
            return database_url
        if database_url.startswith("postgresql://"):
            return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return database_url
    user = env("POSTGRES_USER", "ncoai")
    password = env("POSTGRES_PASSWORD", "ncoai_dev")
    host = env("POSTGRES_HOST", "127.0.0.1")
    port = env("POSTGRES_PORT", "55432")
    db = env("POSTGRES_DB", "ncoai_chat")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def build_redis_url() -> str:
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return redis_url
    host = env("REDIS_HOST", "127.0.0.1")
    port = env("REDIS_PORT", "6379")
    db = env("REDIS_DB", "0")
    return f"redis://{host}:{port}/{db}"


def to_sync_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return database_url


def ensure_database_exists(database_url: str) -> None:
    url = make_url(to_sync_postgres_url(database_url))
    target_db = url.database
    if not target_db:
        return
    admin_url = url.set(database="postgres")
    conninfo = admin_url.render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if cur.fetchone():
                return
            cur.execute(sql.SQL("CREATE DATABASE {}") .format(sql.Identifier(target_db)))


async def main() -> int:
    database_url = build_database_url()
    litellm_database_url = os.getenv("LITELLM_DATABASE_URL", "").strip()

    ensure_database_exists(database_url)
    if litellm_database_url:
        ensure_database_exists(litellm_database_url)

    redis_url = build_redis_url()
    engine = create_async_engine(database_url)
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await ensure_application_schema(database_url)
        policy = await bootstrap_control_plane(
            engine,
            admin_username=env("ADMIN_APP_USERNAME", "admin"),
            admin_password_hash=hash_password(env("ADMIN_APP_PASSWORD", "admin123")),
            total_token_limit=int(env("CHAINLIT_USER_LIMIT_TOKENS", "100000")),
            window_start=parse_iso_datetime(
                os.getenv("BOOTSTRAP_GLOBAL_QUOTA_WINDOW_START"),
            ),
            window_end=parse_iso_datetime(
                os.getenv("BOOTSTRAP_GLOBAL_QUOTA_WINDOW_END"),
            ),
        )
        await sync_global_quota_policy_cache(engine, redis, policy=policy)
        team_token_config = await bootstrap_team_token_auth_config(
            engine,
            public_key_base64=os.getenv("GZCTF_FIXED_GAME_PUBLIC_KEY"),
            private_key_base64=os.getenv("GZCTF_FIXED_GAME_PRIVATE_KEY"),
        )
        await sync_team_token_auth_config_cache(
            engine,
            redis,
            config=team_token_config,
        )
    finally:
        await redis.close()
        await engine.dispose()

    print(
        json.dumps(
            {
                "ok": True,
                "database_url": database_url,
                "litellm_database_url": litellm_database_url,
                "redis_url": redis_url,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
