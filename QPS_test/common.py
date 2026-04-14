from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit


ROOT_DIR = Path(__file__).resolve().parents[1]
QPS_DIR = ROOT_DIR / "QPS_test"
ARTIFACTS_DIR = QPS_DIR / "artifacts"
ENV_DIR = ROOT_DIR / "infra" / "env"
COMMON_ENV_FILE = ENV_DIR / "common.env"
HOST_ENV_FILE = ENV_DIR / "host.env"
SECRETS_ENV_FILE = ENV_DIR / "secrets.env"
DEVCTL_LOG_DIR = ROOT_DIR / ".local" / "devctl" / "logs"

BehaviorType = Literal["normal", "stop", "thread_switch"]
ModeType = Literal["conversation", "legacy_rps"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_host_env() -> dict[str, str]:
    env = os.environ.copy()
    for path in (COMMON_ENV_FILE, HOST_ENV_FILE, SECRETS_ENV_FILE):
        env.update(parse_env_file(path))
    return env


def normalize_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def env_flag(name: str, default: bool, values: dict[str, str] | None = None) -> bool:
    raw = (values or os.environ).get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * ratio
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def directory_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{value} B"


def build_base_url(host: str, port: str, root_path: str) -> str:
    root = root_path.rstrip("/")
    return f"http://{host}:{port}{root}"


def build_socketio_path(chat_base_url: str) -> str:
    split = urlsplit(chat_base_url.rstrip("/"))
    path = split.path.strip("/")
    if path:
        return f"{path}/ws/socket.io"
    return "ws/socket.io"


def build_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def next_run_id(now: datetime | None = None) -> str:
    current = now or utcnow()
    prefix = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def artifact_paths(run_id: str) -> "RunPaths":
    root = ARTIFACTS_DIR / run_id
    smoke_dir = root / "browser_smoke"
    return RunPaths(
        root=root,
        requests_jsonl=root / "requests.jsonl",
        failures_jsonl=root / "failures.jsonl",
        latency_csv=root / "latency.csv",
        summary_json=root / "summary.json",
        report_md=root / "report.md",
        users_json=root / "users.json",
        state_json=root / "runtime_state.json",
        smoke_dir=smoke_dir,
        smoke_summary_json=smoke_dir / "summary.json",
    )


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    requests_jsonl: Path
    failures_jsonl: Path
    latency_csv: Path
    summary_json: Path
    report_md: Path
    users_json: Path
    state_json: Path
    smoke_dir: Path
    smoke_summary_json: Path


@dataclass(frozen=True, slots=True)
class ManagedUserCredential:
    identifier: str
    password: str
    display_name: str
    run_id: str
    created_at: str


@dataclass(slots=True)
class ThreadState:
    local_name: str
    thread_id: str | None = None
    session_used_tokens: int = 0
    request_count: int = 0
    blocked_scope: str | None = None
    quota_blocked_reason: str | None = None
    active_requests: int = 0
    last_updated_at: str | None = None
    rollover_index: int = 1
    topic_name: str = ""
    pending_rollover_reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.blocked_scope == "thread"


@dataclass(slots=True)
class UserState:
    credential: ManagedUserCredential
    auth_cookies: dict[str, str] = field(default_factory=dict)
    threads: list[ThreadState] = field(default_factory=list)
    inflight: int = 0
    next_request_seq: int = 1
    completed_requests: int = 0
    failed_requests: int = 0
    total_used_tokens: int = 0
    blocked_scope: str | None = None
    quota_blocked_reason: str | None = None
    last_thread_index: int = 0

    @property
    def exhausted(self) -> bool:
        return self.blocked_scope == "user"


@dataclass(slots=True)
class RequestRecord:
    run_id: str
    user_id: str
    thread_id: str | None
    session_id: str
    conversation_id: str | None
    behavior_type: BehaviorType
    request_seq: int
    conversation_turn_index: int
    session_rollover_index: int
    request_sent_at: str
    first_token_at: str | None
    task_end_at: str | None
    ttft_ms: float | None
    wall_time_ms: float | None
    generation_duration_ms: float | None
    output_tokens_per_second: float | None
    stop_sent_at: str | None
    success: bool
    failure_type: str | None
    http_or_ws_error: str | None
    input_tokens_estimated: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    completion_tokens_reported_or_estimated: int | None
    total_tokens: int | None
    stopped_by_disturbance: bool
    stop_trigger_tokens_estimated: int | None
    natural_completion: bool
    session_rollover_reason: str | None
    quota_blocked_scope: str | None
    quota_blocked_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrowserSmokeResult:
    success: bool
    login_ok: bool
    stream_ok: bool
    stop_ok: bool
    thread_switch_ok: bool
    screenshots: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LogCapStatus:
    size_bytes: int
    warning_triggered: bool
    abort_triggered: bool


@dataclass(frozen=True, slots=True)
class RunConfig:
    run_id: str
    mode: ModeType
    users: int
    target_rps: int
    per_user_default_inflight: int
    per_user_max_inflight: int
    traffic_profile: str
    log_cap_gb: float
    keep_users: bool
    skip_browser_smoke: bool
    chat_base_url: str
    admin_base_url: str
    database_url: str
    database_async_url: str
    redis_url: str
    system_prompt: str
    tiktoken_encoding: str
    input_token_target: int
    thread_limit_tokens: int
    user_limit_tokens: int
    default_max_completion_tokens: int
    prompt_safety_tokens: int
    request_token_target: int
    stop_ratio: float
    request_timeout_seconds: float
    smoke_timeout_seconds: float
    progress_interval_seconds: float
    login_batch_size: int
    log_cap_warning_ratio: float
    max_global_inflight: int
    conversation_start_jitter_ms: int
    ramp_start_rps: int
    ramp_step_seconds: float
    failure_window_size: int
    failure_abort_ratio: float
    consecutive_failure_abort_count: int
    resource_sample_seconds: float
    duration_seconds: float
    paths: RunPaths
    user_limit_tokens_override: int | None
    thread_limit_tokens_override: int | None
    postgres_container_name: str
    redis_container_name: str
    host_memory_abort_ratio: float
    postgres_memory_abort_ratio: float
    redis_memory_abort_ratio: float
    gpu_memory_abort_ratio: float
    service_health_abort_failures: int
    resource_abort_streak: int

    @property
    def log_cap_bytes(self) -> int:
        return int(self.log_cap_gb * 1024 * 1024 * 1024)

    @property
    def is_conversation_mode(self) -> bool:
        return self.mode == "conversation"

    @property
    def runtime_completion_tokens(self) -> int:
        return min(self.default_max_completion_tokens, 500)

    @property
    def minimum_request_tokens(self) -> int:
        return (
            self.input_token_target
            + self.prompt_safety_tokens
            + self.runtime_completion_tokens
        )


def build_run_config(args: Any) -> RunConfig:
    env = load_host_env()
    run_id = args.run_id or next_run_id()
    paths = artifact_paths(run_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    mode = str(getattr(args, "mode", "conversation")).strip().lower()
    if mode not in {"conversation", "legacy_rps"}:
        raise ValueError(f"Unknown QPS_test mode: {mode}")
    chat_base_url = build_base_url(
        env.get("CHAINLIT_HOST", "127.0.0.1"),
        env.get("CHAINLIT_PORT", "3100"),
        env.get("CHAINLIT_ROOT_PATH", "/chat"),
    )
    admin_base_url = build_base_url(
        env.get("ADMIN_APP_HOST", "127.0.0.1"),
        env.get("ADMIN_APP_PORT", "3200"),
        env.get("ADMIN_APP_ROOT_PATH", "/admin"),
    )
    legacy_request_token_target = getattr(args, "request_token_target", None)
    input_token_target = int(
        getattr(
            args,
            "input_token_target",
            legacy_request_token_target if legacy_request_token_target is not None else 200,
        )
    )
    thread_limit_override = getattr(args, "thread_limit_tokens_override", None)
    user_limit_override = getattr(args, "user_limit_tokens_override", None)
    thread_limit_tokens = int(
        thread_limit_override
        if thread_limit_override is not None
        else env.get("CHAINLIT_THREAD_LIMIT_TOKENS", "2048")
    )
    user_limit_tokens = int(
        user_limit_override
        if user_limit_override is not None
        else env.get("CHAINLIT_USER_LIMIT_TOKENS", "100000")
    )
    per_user_default_inflight = int(getattr(args, "per_user_default_inflight", 1))
    per_user_max_inflight = int(getattr(args, "per_user_max_inflight", 1))
    max_global_inflight = int(getattr(args, "max_global_inflight", 120))
    if mode == "conversation":
        per_user_default_inflight = 1
        per_user_max_inflight = 1
        max_global_inflight = max(max_global_inflight, int(args.users))
    return RunConfig(
        run_id=run_id,
        mode=mode,
        users=int(args.users),
        target_rps=int(args.target_rps),
        per_user_default_inflight=per_user_default_inflight,
        per_user_max_inflight=per_user_max_inflight,
        traffic_profile=str(args.traffic_profile),
        log_cap_gb=float(args.log_cap_gb),
        keep_users=bool(args.keep_users),
        skip_browser_smoke=bool(args.skip_browser_smoke),
        chat_base_url=chat_base_url,
        admin_base_url=admin_base_url,
        database_url=env.get(
            "DATABASE_URL",
            "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
        ),
        database_async_url=normalize_postgres_url(
            env.get(
                "DATABASE_URL",
                "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
            )
        ),
        redis_url=env.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        system_prompt=env.get(
            "CHAINLIT_SYSTEM_PROMPT",
            "You are a concise and helpful assistant.",
        ),
        tiktoken_encoding=env.get("CHAINLIT_QUOTA_TIKTOKEN_ENCODING", "cl100k_base"),
        input_token_target=input_token_target,
        thread_limit_tokens=thread_limit_tokens,
        user_limit_tokens=user_limit_tokens,
        default_max_completion_tokens=int(
            env.get("CHAINLIT_QUOTA_DEFAULT_MAX_COMPLETION_TOKENS", "2048")
        ),
        prompt_safety_tokens=int(env.get("CHAINLIT_QUOTA_PROMPT_SAFETY_TOKENS", "64")),
        request_token_target=int(
            legacy_request_token_target if legacy_request_token_target is not None else 500
        ),
        stop_ratio=float(getattr(args, "stop_ratio", 0.05)),
        request_timeout_seconds=float(getattr(args, "request_timeout_seconds", 240.0)),
        smoke_timeout_seconds=float(getattr(args, "smoke_timeout_seconds", 90.0)),
        progress_interval_seconds=float(
            getattr(args, "progress_interval_seconds", 5.0)
        ),
        login_batch_size=int(getattr(args, "login_batch_size", 10)),
        log_cap_warning_ratio=float(getattr(args, "log_cap_warning_ratio", 0.9)),
        max_global_inflight=max_global_inflight,
        conversation_start_jitter_ms=int(
            getattr(args, "conversation_start_jitter_ms", 3000)
        ),
        ramp_start_rps=int(getattr(args, "ramp_start_rps", 5)),
        ramp_step_seconds=float(getattr(args, "ramp_step_seconds", 30.0)),
        failure_window_size=int(
            getattr(
                args,
                "transport_failure_window",
                getattr(args, "failure_window_size", 30),
            )
        ),
        failure_abort_ratio=float(
            getattr(
                args,
                "transport_failure_abort_ratio",
                getattr(args, "failure_abort_ratio", 0.20),
            )
        ),
        consecutive_failure_abort_count=int(
            getattr(args, "consecutive_failure_abort_count", 12)
        ),
        resource_sample_seconds=float(getattr(args, "resource_sample_seconds", 5.0)),
        duration_seconds=float(getattr(args, "duration_seconds", 0.0)),
        paths=paths,
        user_limit_tokens_override=(
            int(user_limit_override) if user_limit_override is not None else None
        ),
        thread_limit_tokens_override=(
            int(thread_limit_override) if thread_limit_override is not None else None
        ),
        postgres_container_name="ncoai-postgres",
        redis_container_name="ncoai-redis",
        host_memory_abort_ratio=0.92,
        postgres_memory_abort_ratio=0.85,
        redis_memory_abort_ratio=0.80,
        gpu_memory_abort_ratio=0.97,
        service_health_abort_failures=3,
        resource_abort_streak=3,
    )


def write_latency_csv(path: Path, rows: list[RequestRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "request_seq",
                "user_id",
                "thread_id",
                "conversation_id",
                "conversation_turn_index",
                "session_rollover_index",
                "behavior_type",
                "success",
                "ttft_ms",
                "wall_time_ms",
                "generation_duration_ms",
                "output_tokens_per_second",
                "input_tokens_estimated",
                "prompt_tokens",
                "completion_tokens",
                "completion_tokens_reported_or_estimated",
                "total_tokens",
                "stopped_by_disturbance",
                "stop_trigger_tokens_estimated",
                "natural_completion",
                "session_rollover_reason",
                "quota_blocked_scope",
                "failure_type",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.request_seq,
                    row.user_id,
                    row.thread_id or "",
                    row.conversation_id or "",
                    row.conversation_turn_index,
                    row.session_rollover_index,
                    row.behavior_type,
                    "1" if row.success else "0",
                    "" if row.ttft_ms is None else f"{row.ttft_ms:.3f}",
                    "" if row.wall_time_ms is None else f"{row.wall_time_ms:.3f}",
                    ""
                    if row.generation_duration_ms is None
                    else f"{row.generation_duration_ms:.3f}",
                    ""
                    if row.output_tokens_per_second is None
                    else f"{row.output_tokens_per_second:.3f}",
                    row.input_tokens_estimated
                    if row.input_tokens_estimated is not None
                    else "",
                    row.prompt_tokens if row.prompt_tokens is not None else "",
                    row.completion_tokens if row.completion_tokens is not None else "",
                    row.completion_tokens_reported_or_estimated
                    if row.completion_tokens_reported_or_estimated is not None
                    else "",
                    row.total_tokens if row.total_tokens is not None else "",
                    "1" if row.stopped_by_disturbance else "0",
                    row.stop_trigger_tokens_estimated
                    if row.stop_trigger_tokens_estimated is not None
                    else "",
                    "1" if row.natural_completion else "0",
                    row.session_rollover_reason or "",
                    row.quota_blocked_scope or "",
                    row.failure_type or "",
                ]
            )
