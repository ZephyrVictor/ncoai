from __future__ import annotations

import os
from dataclasses import dataclass


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
class DatabaseSettings:
    url: str
    async_url: str


@dataclass(frozen=True, slots=True)
class RedisSettings:
    url: str


@dataclass(frozen=True, slots=True)
class LiteLLMSettings:
    base_url: str
    api_key: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ModelSettings:
    default_model: str
    thinking_model: str
    max_model_len: int
    system_prompt: str
    thinking_enabled: bool


@dataclass(frozen=True, slots=True)
class AuthSettings:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class AdminSettings:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class QuotaSettings:
    thread_limit_tokens: int
    user_limit_tokens: int
    reservation_ttl_seconds: int
    default_max_completion_tokens: int
    prompt_safety_tokens: int
    tiktoken_encoding: str


@dataclass(frozen=True, slots=True)
class AppSettings:
    database: DatabaseSettings
    redis: RedisSettings
    litellm: LiteLLMSettings
    models: ModelSettings
    auth: AuthSettings
    admin: AdminSettings
    quota: QuotaSettings
    chainlit_root_path: str


def load_settings() -> AppSettings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
    )
    return AppSettings(
        database=DatabaseSettings(
            url=database_url,
            async_url=normalize_postgres_url(database_url),
        ),
        redis=RedisSettings(url=build_redis_url()),
        litellm=LiteLLMSettings(
            base_url=os.getenv(
                "CHAINLIT_LITELLM_BASE_URL",
                "http://127.0.0.1:33455/v1",
            ),
            api_key=os.getenv(
                "CHAINLIT_LITELLM_API_KEY",
                os.getenv("LITELLM_MASTER_KEY", "sk-local-dev"),
            ),
            timeout_seconds=float(os.getenv("CHAINLIT_OPENAI_TIMEOUT", "180")),
        ),
        models=ModelSettings(
            default_model="qwen35b_no_think",
            thinking_model="qwen35b_think",
            max_model_len=int(os.getenv("MAX_MODEL_LEN", "8192")),
            system_prompt=os.getenv(
                "CHAINLIT_SYSTEM_PROMPT",
                "You are a concise and helpful assistant.",
            ),
            thinking_enabled=env_flag("CHAINLIT_ENABLE_THINKING", True),
        ),
        auth=AuthSettings(
            username=os.getenv("CHAINLIT_APP_USERNAME", "acceptance"),
            password=os.getenv("CHAINLIT_APP_PASSWORD", "acceptance123"),
        ),
        admin=AdminSettings(
            username=os.getenv("ADMIN_APP_USERNAME", "admin"),
            password=os.getenv("ADMIN_APP_PASSWORD", "admin123"),
        ),
        quota=QuotaSettings(
            thread_limit_tokens=int(os.getenv("CHAINLIT_THREAD_LIMIT_TOKENS", "8192")),
            user_limit_tokens=int(os.getenv("CHAINLIT_USER_LIMIT_TOKENS", "100000")),
            reservation_ttl_seconds=int(
                os.getenv("CHAINLIT_QUOTA_RESERVATION_TTL_SECONDS", "600")
            ),
            default_max_completion_tokens=int(
                os.getenv("CHAINLIT_QUOTA_DEFAULT_MAX_COMPLETION_TOKENS", "2048")
            ),
            prompt_safety_tokens=int(
                os.getenv("CHAINLIT_QUOTA_PROMPT_SAFETY_TOKENS", "64")
            ),
            tiktoken_encoding=os.getenv(
                "CHAINLIT_QUOTA_TIKTOKEN_ENCODING",
                "cl100k_base",
            ),
        ),
        chainlit_root_path=os.getenv("CHAINLIT_ROOT_PATH", ""),
    )


SETTINGS = load_settings()
