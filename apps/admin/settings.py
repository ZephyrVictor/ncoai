from __future__ import annotations

import os
from dataclasses import dataclass


def normalize_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def build_redis_url() -> str:
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return redis_url
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    return f"redis://{host}:{port}/{db}"


@dataclass(frozen=True, slots=True)
class AdminSettings:
    database_url: str
    database_async_url: str
    redis_url: str
    host: str
    port: int
    root_path: str
    session_secret: str
    admin_username: str
    admin_password: str
    bootstrap_global_limit_tokens: int
    bootstrap_global_window_start: str
    bootstrap_global_window_end: str
    model_max_tokens: int
    system_prompt: str
    prompt_safety_tokens: int
    tiktoken_encoding: str
    display_timezone: str


def load_settings() -> AdminSettings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
    )
    return AdminSettings(
        database_url=database_url,
        database_async_url=normalize_postgres_url(database_url),
        redis_url=build_redis_url(),
        host=os.getenv("ADMIN_APP_HOST", "0.0.0.0"),
        port=int(os.getenv("ADMIN_APP_PORT", "3200")),
        root_path=os.getenv("ADMIN_APP_ROOT_PATH", "/admin"),
        session_secret=os.getenv(
            "ADMIN_APP_SESSION_SECRET",
            "admin-local-dev-session-secret-2026",
        ),
        admin_username=os.getenv("ADMIN_APP_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_APP_PASSWORD", "admin123"),
        bootstrap_global_limit_tokens=int(
            os.getenv("CHAINLIT_USER_LIMIT_TOKENS", "100000")
        ),
        bootstrap_global_window_start=os.getenv(
            "BOOTSTRAP_GLOBAL_QUOTA_WINDOW_START",
            "2024-01-01T00:00:00+00:00",
        ),
        bootstrap_global_window_end=os.getenv(
            "BOOTSTRAP_GLOBAL_QUOTA_WINDOW_END",
            "2099-12-31T23:59:59+00:00",
        ),
        model_max_tokens=int(os.getenv("MAX_MODEL_LEN", "8192")),
        system_prompt=os.getenv(
            "CHAINLIT_SYSTEM_PROMPT",
            "You are a concise and helpful assistant.",
        ),
        prompt_safety_tokens=int(
            os.getenv("CHAINLIT_QUOTA_PROMPT_SAFETY_TOKENS", "64")
        ),
        tiktoken_encoding=os.getenv(
            "CHAINLIT_QUOTA_TIKTOKEN_ENCODING",
            "cl100k_base",
        ),
        display_timezone=os.getenv("ADMIN_APP_DISPLAY_TIMEZONE", "Asia/Shanghai"),
    )


SETTINGS = load_settings()
