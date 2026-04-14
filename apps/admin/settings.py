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
    monitoring_prometheus_url: str
    monitoring_loki_url: str
    monitoring_grafana_url: str
    team_token_fixed_game_id: int
    vllm_host: str
    vllm_port: int
    served_model_name: str
    vllm_failover_enabled: bool
    vllm_backup_origin: str
    vllm_primary_request_timeout_seconds: float
    vllm_backup_request_timeout_seconds: float
    vllm_redundancy_probe_interval_seconds: float
    vllm_redundancy_probe_timeout_seconds: float
    vllm_redundancy_restart_cooldown_seconds: int
    vllm_redundancy_restart_max_attempts: int
    vllm_redundancy_restart_window_seconds: int
    vllm_redundancy_restart_command_timeout_seconds: float


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
        model_max_tokens=int(os.getenv("MAX_MODEL_LEN", "2048")),
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
        monitoring_prometheus_url=os.getenv(
            "ADMIN_MONITORING_PROMETHEUS_URL",
            "http://127.0.0.1:9090",
        ),
        monitoring_loki_url=os.getenv(
            "ADMIN_MONITORING_LOKI_URL",
            "http://127.0.0.1:3101",
        ),
        monitoring_grafana_url=os.getenv(
            "ADMIN_MONITORING_GRAFANA_URL",
            "/grafana/",
        ),
        team_token_fixed_game_id=int(os.getenv("GZCTF_FIXED_GAME_ID", "0")),
        vllm_host=os.getenv("VLLM_HOST", "127.0.0.1"),
        vllm_port=int(os.getenv("VLLM_PORT", "12233")),
        served_model_name=os.getenv("SERVED_MODEL_NAME", "qwen35b"),
        vllm_failover_enabled=os.getenv("VLLM_FAILOVER_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"},
        vllm_backup_origin=os.getenv("VLLM_BACKUP_ORIGIN", "").strip(),
        vllm_primary_request_timeout_seconds=float(
            os.getenv("VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS", "2.5")
        ),
        vllm_backup_request_timeout_seconds=float(
            os.getenv("VLLM_BACKUP_REQUEST_TIMEOUT_SECONDS", "5.0")
        ),
        vllm_redundancy_probe_interval_seconds=float(
            os.getenv("ADMIN_VLLM_REDUNDANCY_PROBE_INTERVAL_SECONDS", "600")
        ),
        vllm_redundancy_probe_timeout_seconds=float(
            os.getenv("ADMIN_VLLM_REDUNDANCY_PROBE_TIMEOUT_SECONDS", "3")
        ),
        vllm_redundancy_restart_cooldown_seconds=int(
            os.getenv("ADMIN_VLLM_REDUNDANCY_RESTART_COOLDOWN_SECONDS", "120")
        ),
        vllm_redundancy_restart_max_attempts=int(
            os.getenv("ADMIN_VLLM_REDUNDANCY_RESTART_MAX_ATTEMPTS", "3")
        ),
        vllm_redundancy_restart_window_seconds=int(
            os.getenv("ADMIN_VLLM_REDUNDANCY_RESTART_WINDOW_SECONDS", "900")
        ),
        vllm_redundancy_restart_command_timeout_seconds=float(
            os.getenv("ADMIN_VLLM_REDUNDANCY_RESTART_COMMAND_TIMEOUT_SECONDS", "180")
        ),
    )


SETTINGS = load_settings()
