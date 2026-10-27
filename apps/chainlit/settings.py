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


def per_worker_capacity(total: int, worker_count: int, *, minimum: int = 1) -> int:
    if worker_count <= 1:
        return max(total, minimum)
    if total <= 0:
        return minimum
    return max(total // worker_count, minimum)


@dataclass(frozen=True, slots=True)
class DatabasePoolSettings:
    size: int
    max_overflow: int
    timeout_seconds: float
    recycle_seconds: int
    pre_ping: bool
    use_lifo: bool


@dataclass(frozen=True, slots=True)
class DataCacheSettings:
    enabled: bool
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str
    async_url: str
    pool: DatabasePoolSettings


@dataclass(frozen=True, slots=True)
class RedisSettings:
    url: str


@dataclass(frozen=True, slots=True)
class LiteLLMSettings:
    base_url: str
    api_key: str
    timeout_seconds: float
    max_retries: int
    max_connections: int
    max_keepalive_connections: int
    max_concurrent_requests: int
    queue_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    worker_count: int
    worker_index: int
    worker_id: str
    stream_snapshot_interval_seconds: float


@dataclass(frozen=True, slots=True)
class GlobalAdmissionSettings:
    max_inflight: int
    queue_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class MetadataFlushSettings:
    interval_seconds: float


@dataclass(frozen=True, slots=True)
class ModelSettings:
    default_model: str
    default_model_display_name: str
    thinking_model: str
    thinking_model_display_name: str
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
class TeamTokenAuthSettings:
    mode: str
    fixed_game_id: int
    fixed_game_public_key: str
    fixed_game_private_key: str


@dataclass(frozen=True, slots=True)
class CapsSettings:
    enabled: bool
    storage_root: str
    ftp_host: str
    ftp_port: int
    ftp_username: str
    ftp_password: str
    ftp_passive_port_start: int
    ftp_passive_port_end: int
    ftp_public_host: str | None
    ftp_tls_enabled: bool
    ftp_tls_certfile: str | None
    ftp_tls_keyfile: str | None
    ftp_tls_control_required: bool
    ftp_tls_data_required: bool

    @property
    def passive_ports(self) -> tuple[int, ...]:
        start = self.ftp_passive_port_start
        end = self.ftp_passive_port_end
        if start <= 0 or end <= 0 or start > 65535 or end > 65535:
            raise ValueError("CAPS FTP passive port range must use valid TCP ports")
        if end < start:
            raise ValueError(
                "CAPS_FTP_PASSIVE_PORT_END must be greater than or equal to CAPS_FTP_PASSIVE_PORT_START"
            )
        return tuple(range(start, end + 1))


@dataclass(frozen=True, slots=True)
class AppSettings:
    database: DatabaseSettings
    data_cache: DataCacheSettings
    redis: RedisSettings
    litellm: LiteLLMSettings
    runtime: RuntimeSettings
    admission: GlobalAdmissionSettings
    metadata_flush: MetadataFlushSettings
    models: ModelSettings
    auth: AuthSettings
    admin: AdminSettings
    quota: QuotaSettings
    team_token_auth: TeamTokenAuthSettings
    caps: CapsSettings
    chainlit_root_path: str


def load_settings() -> AppSettings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
    )
    worker_count = max(int(os.getenv("CHAINLIT_WORKER_COUNT", "1")), 1)
    worker_index = max(int(os.getenv("CHAINLIT_WORKER_INDEX", "1")), 1)
    worker_id = os.getenv(
        "CHAINLIT_WORKER_ID",
        f"chainlit-{worker_index}",
    ).strip() or f"chainlit-{worker_index}"
    max_num_seqs = max(int(os.getenv("MAX_NUM_SEQS", "32")), 1)
    default_global_admission_limit = max_num_seqs
    return AppSettings(
        database=DatabaseSettings(
            url=database_url,
            async_url=normalize_postgres_url(database_url),
            pool=DatabasePoolSettings(
                size=per_worker_capacity(
                    int(os.getenv("CHAINLIT_DB_POOL_SIZE", "128")),
                    worker_count,
                ),
                max_overflow=per_worker_capacity(
                    int(os.getenv("CHAINLIT_DB_MAX_OVERFLOW", "64")),
                    worker_count,
                    minimum=0,
                ),
                timeout_seconds=float(
                    os.getenv("CHAINLIT_DB_POOL_TIMEOUT_SECONDS", "15")
                ),
                recycle_seconds=int(
                    os.getenv("CHAINLIT_DB_POOL_RECYCLE_SECONDS", "1800")
                ),
                pre_ping=env_flag("CHAINLIT_DB_POOL_PRE_PING", True),
                use_lifo=env_flag("CHAINLIT_DB_POOL_USE_LIFO", True),
            ),
        ),
        data_cache=DataCacheSettings(
            enabled=env_flag("CHAINLIT_DATA_CACHE_ENABLED", True),
            ttl_seconds=int(os.getenv("CHAINLIT_DATA_CACHE_TTL_SECONDS", "15")),
        ),
        redis=RedisSettings(url=build_redis_url()),
        runtime=RuntimeSettings(
            worker_count=worker_count,
            worker_index=worker_index,
            worker_id=worker_id,
            stream_snapshot_interval_seconds=max(
                float(
                    os.getenv(
                        "CHAINLIT_STREAM_SNAPSHOT_INTERVAL_SECONDS",
                        "10",
                    )
                ),
                0.1,
            ),
        ),
        admission=GlobalAdmissionSettings(
            max_inflight=max(
                int(
                    os.getenv(
                        "CHAINLIT_GLOBAL_ADMISSION_MAX_INFLIGHT",
                        str(default_global_admission_limit),
                    )
                ),
                1,
            ),
            queue_timeout_seconds=max(
                float(
                    os.getenv(
                        "CHAINLIT_GLOBAL_ADMISSION_QUEUE_TIMEOUT_SECONDS",
                        os.getenv("CHAINLIT_LITELLM_QUEUE_TIMEOUT_SECONDS", "120"),
                    )
                ),
                0.1,
            ),
        ),
        metadata_flush=MetadataFlushSettings(
            interval_seconds=max(
                float(
                    os.getenv(
                        "CHAINLIT_METADATA_FLUSH_INTERVAL_SECONDS",
                        "5",
                    )
                ),
                0.1,
            ),
        ),
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
            max_retries=max(int(os.getenv("CHAINLIT_OPENAI_MAX_RETRIES", "0")), 0),
            max_connections=per_worker_capacity(
                int(os.getenv("CHAINLIT_OPENAI_MAX_CONNECTIONS", "256")),
                worker_count,
            ),
            max_keepalive_connections=per_worker_capacity(
                int(
                    os.getenv(
                        "CHAINLIT_OPENAI_MAX_KEEPALIVE_CONNECTIONS",
                        "128",
                    )
                ),
                worker_count,
            ),
            max_concurrent_requests=per_worker_capacity(
                int(os.getenv("CHAINLIT_LITELLM_MAX_CONCURRENT_REQUESTS", "160")),
                worker_count,
            ),
            queue_timeout_seconds=max(
                float(os.getenv("CHAINLIT_LITELLM_QUEUE_TIMEOUT_SECONDS", "120")),
                0.1,
            ),
        ),
        models=ModelSettings(
            default_model=os.getenv("CHAINLIT_DEFAULT_MODEL", "qwen35b_no_think"),
            default_model_display_name=os.getenv(
                "CHAINLIT_DEFAULT_MODEL_DISPLAY_NAME",
                "chat",
            ),
            thinking_model=os.getenv("CHAINLIT_THINKING_MODEL", "qwen35b_think"),
            thinking_model_display_name=os.getenv(
                "CHAINLIT_THINKING_MODEL_DISPLAY_NAME",
                "thinking",
            ),
            max_model_len=int(os.getenv("MAX_MODEL_LEN", "2048")),
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
            thread_limit_tokens=int(os.getenv("CHAINLIT_THREAD_LIMIT_TOKENS", "2048")),
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
        team_token_auth=TeamTokenAuthSettings(
            mode=os.getenv("CHAT_AUTH_MODE", "managed_user").strip().lower(),
            fixed_game_id=int(os.getenv("GZCTF_FIXED_GAME_ID", "0")),
            fixed_game_public_key=os.getenv("GZCTF_FIXED_GAME_PUBLIC_KEY", "").strip(),
            fixed_game_private_key=os.getenv(
                "GZCTF_FIXED_GAME_PRIVATE_KEY",
                "",
            ).strip(),
        ),
        caps=CapsSettings(
            enabled=env_flag("CAPS_ENABLED", True),
            storage_root=os.getenv("CAPS_STORAGE_ROOT", "/data/ncoai/caps").strip(),
            ftp_host=os.getenv("CAPS_FTP_HOST", "0.0.0.0").strip(),
            ftp_port=int(os.getenv("CAPS_FTP_PORT", "3221")),
            ftp_username=os.getenv("CAPS_FTP_USERNAME", "").strip(),
            ftp_password=os.getenv("CAPS_FTP_PASSWORD", "").strip(),
            ftp_passive_port_start=int(
                os.getenv("CAPS_FTP_PASSIVE_PORT_START", "41000")
            ),
            ftp_passive_port_end=int(
                os.getenv("CAPS_FTP_PASSIVE_PORT_END", "41099")
            ),
            ftp_public_host=(
                os.getenv("CAPS_FTP_PUBLIC_HOST", "").strip() or None
            ),
            ftp_tls_enabled=env_flag("CAPS_FTP_TLS_ENABLED", False),
            ftp_tls_certfile=(
                os.getenv(
                    "CAPS_FTP_TLS_CERTFILE",
                    os.getenv("NGINX_SSL_CERT", ""),
                ).strip()
                or None
            ),
            ftp_tls_keyfile=(
                os.getenv(
                    "CAPS_FTP_TLS_KEYFILE",
                    os.getenv("NGINX_SSL_KEY", ""),
                ).strip()
                or None
            ),
            ftp_tls_control_required=env_flag(
                "CAPS_FTP_TLS_CONTROL_REQUIRED",
                True,
            ),
            ftp_tls_data_required=env_flag(
                "CAPS_FTP_TLS_DATA_REQUIRED",
                True,
            ),
        ),
        chainlit_root_path=os.getenv("CHAINLIT_ROOT_PATH", ""),
    )


SETTINGS = load_settings()
