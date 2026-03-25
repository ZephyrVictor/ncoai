from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import tiktoken
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from database import create_app_engine
from ncoai_shared.controlplane import ControlPlaneRepository
from ncoai_shared.quota_cache import (
    clear_cached_global_quota_policy,
    load_cached_global_quota_policy,
    store_cached_global_quota_policy,
)
from quota_repository import QuotaRepository
from settings import SETTINGS

LOGGER = logging.getLogger("ncoai.quota")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


@dataclass(slots=True)
class QuotaConfig:
    limit_tokens: int
    policy_version: int = 0
    window_start: str | None = None
    window_end: str | None = None
    updated_at: str | None = None
    status: str = "ok"
    message: str | None = None


@dataclass(slots=True)
class QuotaSummary:
    used_tokens: int
    reserved_tokens: int
    limit_tokens: int
    policy_version: int = 0
    updated_at: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    status: str = "ok"
    message: str | None = None

    @property
    def remaining_tokens(self) -> int:
        return max(self.limit_tokens - self.used_tokens, 0)

    @property
    def available_tokens(self) -> int:
        return max(self.limit_tokens - self.used_tokens - self.reserved_tokens, 0)

    def to_metadata_summary(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "reserved_tokens": self.reserved_tokens,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": self.remaining_tokens,
            "policy_version": self.policy_version,
            "updated_at": self.updated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "status": self.status,
            "message": self.message,
        }

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "reserved_tokens": self.reserved_tokens,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": self.remaining_tokens,
            "policy_version": self.policy_version,
            "updated_at": self.updated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "status": self.status,
            "message": self.message,
        }


@dataclass(slots=True)
class QuotaReservation:
    request_id: str
    step_id: str
    thread_id: str
    user_identifier: str
    prompt_tokens: int
    reserved_tokens: int
    allowed_completion_tokens: int
    thread_summary: QuotaSummary
    user_summary: QuotaSummary


@dataclass(slots=True)
class QuotaSettlement:
    request_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage_source: str
    thread_summary: QuotaSummary
    user_summary: QuotaSummary

    def to_message_metadata(self) -> dict[str, Any]:
        return {
            "request_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "session_used_tokens": self.thread_summary.used_tokens,
            "session_limit_tokens": self.thread_summary.limit_tokens,
            "user_used_tokens": self.user_summary.used_tokens,
            "user_limit_tokens": self.user_summary.limit_tokens,
            "updated_at": self.thread_summary.updated_at or self.user_summary.updated_at,
        }


class QuotaExceededError(RuntimeError):
    def __init__(
        self,
        scope: str,
        message: str,
        thread_summary: QuotaSummary,
        user_summary: QuotaSummary,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.thread_summary = thread_summary
        self.user_summary = user_summary


class DatabaseQuotaConfigProvider:
    def __init__(
        self,
        repository: ControlPlaneRepository,
        *,
        thread_limit_tokens: int,
    ) -> None:
        self._repository = repository
        self._thread_limit_tokens = thread_limit_tokens
        self._redis: Redis | None = None

    def bind_redis(self, redis: Redis) -> None:
        self._redis = redis

    async def get_thread_limit(
        self, thread_id: str, user_identifier: str
    ) -> QuotaConfig:
        del thread_id, user_identifier
        return QuotaConfig(limit_tokens=self._thread_limit_tokens)

    async def get_user_limit(self, user_identifier: str, now: datetime) -> QuotaConfig:
        del user_identifier
        policy = await self._get_global_policy()
        if policy is None:
            LOGGER.warning("No global quota policy configured")
            return QuotaConfig(
                limit_tokens=0,
                policy_version=0,
                updated_at=utcnow_iso(),
                status="missing_policy",
                message="当前没有可用配额配置",
            )
        if not policy.get("is_active"):
            return QuotaConfig(
                limit_tokens=int(policy["total_token_limit"]),
                policy_version=int(policy.get("policy_version") or 0),
                window_start=policy.get("window_start"),
                window_end=policy.get("window_end"),
                updated_at=policy.get("updated_at"),
                status="missing_policy",
                message="当前没有可用配额配置",
            )

        window_start = policy.get("window_start")
        window_end = policy.get("window_end")
        start_dt = self._deserialize_datetime(window_start)
        end_dt = self._deserialize_datetime(window_end)
        if (start_dt and now < start_dt) or (end_dt and now > end_dt):
            return QuotaConfig(
                limit_tokens=int(policy["total_token_limit"]),
                policy_version=int(policy.get("policy_version") or 0),
                window_start=window_start,
                window_end=window_end,
                updated_at=policy.get("updated_at"),
                status="window_closed",
                message="当前不在配额生效时间内",
            )
        return QuotaConfig(
            limit_tokens=int(policy["total_token_limit"]),
            policy_version=int(policy.get("policy_version") or 0),
            window_start=window_start,
            window_end=window_end,
            updated_at=policy.get("updated_at"),
        )

    async def _get_global_policy(self) -> dict[str, Any] | None:
        if self._redis is not None:
            cached = await load_cached_global_quota_policy(self._redis)
            if cached is not None:
                return cached.to_dict()
        policy = await self._repository.get_global_quota_policy()
        if policy and self._redis is not None:
            await store_cached_global_quota_policy(self._redis, policy)
        return policy

    def _deserialize_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))


class QuotaService:
    RESERVE_SCRIPT = """
local thread_key = KEYS[1]
local user_key = KEYS[2]
local request_key = KEYS[3]
local now = ARGV[1]
local thread_limit = tonumber(ARGV[2])
local user_limit = tonumber(ARGV[3])
local reserve_tokens = tonumber(ARGV[4])
local ttl_seconds = tonumber(ARGV[5])
local prompt_tokens = tonumber(ARGV[6])
local step_id = ARGV[7]
local thread_id = ARGV[8]
local user_identifier = ARGV[9]

local thread_used = tonumber(redis.call("HGET", thread_key, "used_tokens") or "0")
local thread_reserved = tonumber(redis.call("HGET", thread_key, "reserved_tokens") or "0")
local user_used = tonumber(redis.call("HGET", user_key, "used_tokens") or "0")
local user_reserved = tonumber(redis.call("HGET", user_key, "reserved_tokens") or "0")

if thread_used + thread_reserved + reserve_tokens > thread_limit then
  return {0, "thread", thread_used, thread_reserved, thread_limit, user_used, user_reserved, user_limit}
end

if user_used + user_reserved + reserve_tokens > user_limit then
  return {0, "user", thread_used, thread_reserved, thread_limit, user_used, user_reserved, user_limit}
end

redis.call("HSET", thread_key,
  "used_tokens", thread_used,
  "reserved_tokens", thread_reserved + reserve_tokens,
  "limit_tokens", thread_limit,
  "updated_at", now
)
redis.call("HSET", user_key,
  "used_tokens", user_used,
  "reserved_tokens", user_reserved + reserve_tokens,
  "limit_tokens", user_limit,
  "updated_at", now
)
redis.call("HSET", request_key,
  "status", "reserved",
  "reserved_tokens", reserve_tokens,
  "prompt_tokens", prompt_tokens,
  "step_id", step_id,
  "thread_id", thread_id,
  "user_identifier", user_identifier,
  "created_at", now
)
redis.call("EXPIRE", request_key, ttl_seconds)
return {1, "", thread_used, thread_reserved + reserve_tokens, thread_limit, user_used, user_reserved + reserve_tokens, user_limit}
"""

    RELEASE_SCRIPT = """
local thread_key = KEYS[1]
local user_key = KEYS[2]
local request_key = KEYS[3]
local now = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local status = ARGV[3]

local reserved_tokens = tonumber(redis.call("HGET", request_key, "reserved_tokens") or "0")
if reserved_tokens == 0 then
  return {0}
end

local thread_reserved = tonumber(redis.call("HGET", thread_key, "reserved_tokens") or "0")
local user_reserved = tonumber(redis.call("HGET", user_key, "reserved_tokens") or "0")

redis.call("HSET", thread_key, "reserved_tokens", math.max(thread_reserved - reserved_tokens, 0), "updated_at", now)
redis.call("HSET", user_key, "reserved_tokens", math.max(user_reserved - reserved_tokens, 0), "updated_at", now)
redis.call("HSET", request_key, "status", status, "settled_at", now)
redis.call("EXPIRE", request_key, ttl_seconds)
return {1}
"""

    SETTLE_SCRIPT = """
local thread_key = KEYS[1]
local user_key = KEYS[2]
local request_key = KEYS[3]
local now = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local total_tokens = tonumber(ARGV[3])
local prompt_tokens = tonumber(ARGV[4])
local completion_tokens = tonumber(ARGV[5])

local reserved_tokens = tonumber(redis.call("HGET", request_key, "reserved_tokens") or "0")
if reserved_tokens == 0 then
  return {0}
end

local request_status = redis.call("HGET", request_key, "status")
if request_status == "settled" then
  return {2}
end

local thread_used = tonumber(redis.call("HGET", thread_key, "used_tokens") or "0")
local thread_reserved = tonumber(redis.call("HGET", thread_key, "reserved_tokens") or "0")
local user_used = tonumber(redis.call("HGET", user_key, "used_tokens") or "0")
local user_reserved = tonumber(redis.call("HGET", user_key, "reserved_tokens") or "0")

redis.call("HSET", thread_key,
  "used_tokens", thread_used + total_tokens,
  "reserved_tokens", math.max(thread_reserved - reserved_tokens, 0),
  "updated_at", now
)
redis.call("HSET", user_key,
  "used_tokens", user_used + total_tokens,
  "reserved_tokens", math.max(user_reserved - reserved_tokens, 0),
  "updated_at", now
)
redis.call("HSET", request_key,
  "status", "settled",
  "prompt_tokens", prompt_tokens,
  "completion_tokens", completion_tokens,
  "total_tokens", total_tokens,
  "settled_at", now
)
redis.call("EXPIRE", request_key, ttl_seconds)
return {1}
"""

    def __init__(self, database_url: str, settings=SETTINGS) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_app_engine(database_url)
        self._repository = QuotaRepository(self._engine)
        self._control_plane_repository = ControlPlaneRepository(self._engine)
        self._config_provider = DatabaseQuotaConfigProvider(
            self._control_plane_repository,
            thread_limit_tokens=settings.models.max_model_len,
        )
        self._reservation_ttl_seconds = settings.quota.reservation_ttl_seconds
        self._default_max_completion_tokens = (
            settings.quota.default_max_completion_tokens
        )
        self._prompt_safety_tokens = settings.quota.prompt_safety_tokens
        self._encoder = tiktoken.get_encoding(settings.quota.tiktoken_encoding)
        self._minimum_user_request_tokens = (
            self.count_messages_tokens(
                [
                    {"role": "system", "content": settings.models.system_prompt},
                    {"role": "user", "content": ""},
                ]
            )
            + self._prompt_safety_tokens
        )
        self._redis: Redis | None = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._redis = Redis.from_url(
            self._settings.redis.url, decode_responses=True
        )
        await self._redis.ping()
        self._config_provider.bind_redis(self._redis)
        await self.reconcile_stale_reservations()
        await self.rebuild_hot_cache()
        self._initialized = True

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
        await self._engine.dispose()

    async def _ensure_ready(self) -> None:
        if not self._initialized:
            await self.initialize()
        if self._redis is None:
            raise RuntimeError("Quota Redis client is not available")

    def count_text_tokens(self, value: str) -> int:
        if not value:
            return 0
        return len(self._encoder.encode(value))

    def count_messages_tokens(self, messages: Sequence[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            total += 4
            total += self.count_text_tokens(str(message.get("role", "")))
            content = message.get("content")
            if isinstance(content, str):
                total += self.count_text_tokens(content)
            elif content is not None:
                total += self.count_text_tokens(
                    json.dumps(content, ensure_ascii=False, sort_keys=True)
                )
        return total + 2

    def _format_tokens(self, value: int) -> str:
        return f"{max(value, 0):,}"

    def _thread_blocked_message(
        self,
        summary: QuotaSummary,
        prompt_budget_tokens: int,
    ) -> str:
        if summary.available_tokens <= 0:
            return "当前对话额度已用尽"
        required_tokens = prompt_budget_tokens + 1
        return (
            "当前对话剩余额度不足以容纳下一轮上下文"
            f"（剩余 {self._format_tokens(summary.available_tokens)} tok，"
            f"至少需要 {self._format_tokens(required_tokens)} tok），请新建对话"
        )

    def _user_blocked_message(
        self,
        summary: QuotaSummary,
        minimum_required_tokens: int | None = None,
    ) -> str:
        if summary.available_tokens <= 0:
            return "用户总额度已用尽"
        if minimum_required_tokens is None:
            minimum_required_tokens = self._minimum_user_request_tokens
        return (
            "用户总额度剩余不足以发起下一次请求"
            f"（剩余 {self._format_tokens(summary.available_tokens)} tok，"
            f"至少需要 {self._format_tokens(minimum_required_tokens)} tok）"
        )

    async def reserve(
        self,
        thread_id: str,
        user_identifier: str,
        step_id: str,
        messages: Sequence[dict[str, Any]],
    ) -> QuotaReservation:
        await self._ensure_ready()

        prompt_tokens = self.count_messages_tokens(messages)
        prompt_budget_tokens = prompt_tokens + self._prompt_safety_tokens
        now = utcnow()
        thread_config = await self._config_provider.get_thread_limit(
            thread_id, user_identifier
        )
        user_config = await self._config_provider.get_user_limit(user_identifier, now)

        thread_summary = await self._ensure_thread_hot(thread_id, thread_config)
        user_summary = await self._ensure_user_hot(user_identifier, user_config)

        if user_config.status != "ok":
            raise QuotaExceededError(
                "window" if user_config.status == "window_closed" else "config",
                user_config.message or "当前没有可用配额配置",
                thread_summary,
                user_summary,
            )

        allowed_completion_tokens = min(
            self._default_max_completion_tokens,
            thread_summary.available_tokens - prompt_budget_tokens,
            user_summary.available_tokens - prompt_budget_tokens,
        )

        if allowed_completion_tokens <= 0:
            scope = (
                "thread"
                if thread_summary.available_tokens - prompt_budget_tokens <= 0
                else "user"
            )
            message = (
                self._thread_blocked_message(thread_summary, prompt_budget_tokens)
                if scope == "thread"
                else self._user_blocked_message(user_summary)
            )
            raise QuotaExceededError(scope, message, thread_summary, user_summary)

        reserved_tokens = prompt_budget_tokens + allowed_completion_tokens
        request_id = str(uuid.uuid4())

        thread_key = self._thread_key(thread_id)
        user_key = self._user_key(user_identifier)
        request_key = self._request_key(request_id)
        assert self._redis is not None
        result = await self._redis.eval(
            self.RESERVE_SCRIPT,
            3,
            thread_key,
            user_key,
            request_key,
            utcnow_iso(),
            thread_config.limit_tokens,
            user_config.limit_tokens,
            reserved_tokens,
            self._reservation_ttl_seconds,
            prompt_tokens,
            step_id,
            thread_id,
            user_identifier,
        )

        if not result or int(result[0]) != 1:
            scope = result[1] if len(result) > 1 else "thread"
            latest_thread = await self._get_hot_summary(thread_key, thread_config)
            latest_user = await self._get_hot_summary(user_key, user_config)
            message = (
                self._thread_blocked_message(latest_thread, prompt_budget_tokens)
                if scope == "thread"
                else self._user_blocked_message(latest_user)
            )
            raise QuotaExceededError(scope, message, latest_thread, latest_user)

        try:
            persisted_thread, persisted_user = await self._persist_reservation(
                request_id=request_id,
                step_id=step_id,
                thread_id=thread_id,
                user_identifier=user_identifier,
                reserved_tokens=reserved_tokens,
                prompt_tokens=prompt_tokens,
                thread_config=thread_config,
                user_config=user_config,
            )
        except Exception:
            await self._redis.eval(
                self.RELEASE_SCRIPT,
                3,
                thread_key,
                user_key,
                request_key,
                utcnow_iso(),
                self._reservation_ttl_seconds,
                "released",
            )
            raise

        return QuotaReservation(
            request_id=request_id,
            step_id=step_id,
            thread_id=thread_id,
            user_identifier=user_identifier,
            prompt_tokens=prompt_tokens,
            reserved_tokens=reserved_tokens,
            allowed_completion_tokens=allowed_completion_tokens,
            thread_summary=persisted_thread,
            user_summary=persisted_user,
        )

    async def settle(
        self,
        reservation: QuotaReservation,
        provider_request_id: str | None,
        assistant_content: str,
        usage: Any | None,
    ) -> QuotaSettlement:
        await self._ensure_ready()

        prompt_tokens, completion_tokens, total_tokens, usage_source = self._resolve_usage(
            reservation.prompt_tokens, assistant_content, usage
        )
        thread_summary, user_summary = await self._persist_settlement(
            request_id=reservation.request_id,
            provider_request_id=provider_request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_source=usage_source,
        )
        assert self._redis is not None
        await self._redis.eval(
            self.SETTLE_SCRIPT,
            3,
            self._thread_key(reservation.thread_id),
            self._user_key(reservation.user_identifier),
            self._request_key(reservation.request_id),
            utcnow_iso(),
            self._reservation_ttl_seconds,
            total_tokens,
            prompt_tokens,
            completion_tokens,
        )
        thread_config = await self._config_provider.get_thread_limit(
            reservation.thread_id,
            reservation.user_identifier,
        )
        user_config = await self._config_provider.get_user_limit(
            reservation.user_identifier,
            utcnow(),
        )
        thread_summary = await self._get_hot_summary(
            self._thread_key(reservation.thread_id),
            thread_config,
        )
        user_summary = await self._get_hot_summary(
            self._user_key(reservation.user_identifier),
            user_config,
        )
        if user_config.status == "ok":
            user_summary.used_tokens = await self._repository.sum_user_window_usage(
                reservation.user_identifier,
                window_start=self._deserialize_datetime(user_config.window_start),
                window_end=self._deserialize_datetime(user_config.window_end),
            )
            user_summary.limit_tokens = user_config.limit_tokens
            user_summary.policy_version = user_config.policy_version
            user_summary.window_start = user_config.window_start
            user_summary.window_end = user_config.window_end
            user_summary.updated_at = utcnow_iso()
        thread_summary.used_tokens = max(
            thread_summary.used_tokens,
            reservation.thread_summary.used_tokens + total_tokens,
        )
        user_summary.used_tokens = max(
            user_summary.used_tokens,
            reservation.user_summary.used_tokens + total_tokens,
        )
        await self._set_hot_summary(
            self._thread_key(reservation.thread_id),
            thread_summary,
        )
        await self._set_hot_summary(
            self._user_key(reservation.user_identifier),
            user_summary,
        )
        user_summary = self._apply_user_summary_status(
            user_summary,
            user_config,
        )
        await self._persist_thread_metadata(reservation.thread_id, thread_summary)
        return QuotaSettlement(
            request_id=reservation.request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_source=usage_source,
            thread_summary=thread_summary,
            user_summary=user_summary,
        )

    async def release(
        self, reservation: QuotaReservation, status: str = "released"
    ) -> tuple[QuotaSummary, QuotaSummary]:
        await self._ensure_ready()
        thread_summary, user_summary = await self._persist_release(
            request_id=reservation.request_id,
            status=status,
        )
        assert self._redis is not None
        await self._redis.eval(
            self.RELEASE_SCRIPT,
            3,
            self._thread_key(reservation.thread_id),
            self._user_key(reservation.user_identifier),
            self._request_key(reservation.request_id),
            utcnow_iso(),
            self._reservation_ttl_seconds,
            status,
        )
        await self._persist_thread_metadata(reservation.thread_id, thread_summary)
        return thread_summary, user_summary

    async def get_user_quota(self, user_identifier: str) -> QuotaSummary:
        await self._ensure_ready()
        config = await self._config_provider.get_user_limit(user_identifier, utcnow())
        return self._apply_user_summary_status(
            await self._ensure_user_hot(user_identifier, config),
            config,
        )

    async def mark_thread_blocked(
        self,
        thread_id: str,
        summary: QuotaSummary,
        message: str,
    ) -> QuotaSummary:
        blocked_summary = QuotaSummary(
            used_tokens=summary.used_tokens,
            reserved_tokens=summary.reserved_tokens,
            limit_tokens=summary.limit_tokens,
            policy_version=summary.policy_version,
            updated_at=utcnow_iso(),
            window_start=summary.window_start,
            window_end=summary.window_end,
            status="blocked",
            message=message,
        )
        await self._persist_thread_metadata(thread_id, blocked_summary)
        return blocked_summary

    async def rebuild_hot_cache(self) -> None:
        await self._ensure_ready_for_cache_only()
        assert self._redis is not None
        policy = await self._control_plane_repository.get_global_quota_policy()
        if policy is None:
            await clear_cached_global_quota_policy(self._redis)
        else:
            await store_cached_global_quota_policy(self._redis, policy)
        for row in await self._repository.list_thread_summaries():
            await self._redis.hset(
                self._thread_key(row["thread_id"]),
                mapping={
                    "used_tokens": int(row["used_tokens"] or 0),
                    "reserved_tokens": int(row["reserved_tokens"] or 0),
                    "limit_tokens": int(row["limit_tokens"] or 0),
                    "updated_at": self._serialize_datetime(row["updated_at"]),
                },
            )

        for row in await self._repository.list_user_summaries():
            await self._redis.hset(
                self._user_key(row["user_identifier"]),
                mapping={
                    "used_tokens": int(row["used_tokens"] or 0),
                    "reserved_tokens": int(row["reserved_tokens"] or 0),
                    "limit_tokens": int(row["limit_tokens"] or 0),
                    "policy_version": int(row.get("policy_version") or 0),
                    "updated_at": self._serialize_datetime(row["updated_at"]),
                    "window_start": self._serialize_datetime(row.get("window_start")) or "",
                    "window_end": self._serialize_datetime(row.get("window_end")) or "",
                },
            )

    async def reconcile_stale_reservations(self) -> None:
        cutoff = utcnow() - timedelta(seconds=self._reservation_ttl_seconds)
        await self._repository.expire_stale_reservations(cutoff=cutoff, now=utcnow())

    async def _ensure_ready_for_cache_only(self) -> None:
        if self._redis is None:
            self._redis = Redis.from_url(
                self._settings.redis.url, decode_responses=True
            )
            await self._redis.ping()

    async def _ensure_thread_hot(
        self, thread_id: str, config: QuotaConfig
    ) -> QuotaSummary:
        key = self._thread_key(thread_id)
        summary = await self._get_hot_summary(key, config)
        if summary.limit_tokens != config.limit_tokens:
            summary.limit_tokens = config.limit_tokens
            summary.updated_at = utcnow_iso()
            await self._set_hot_summary(key, summary)
        if summary.updated_at is not None or summary.used_tokens or summary.reserved_tokens:
            return summary
        db_summary = await self._load_thread_summary(thread_id, config)
        await self._set_hot_summary(key, db_summary)
        return db_summary

    async def _ensure_user_hot(
        self, user_identifier: str, config: QuotaConfig
    ) -> QuotaSummary:
        if config.status == "missing_policy":
            return QuotaSummary(
                used_tokens=0,
                reserved_tokens=0,
                limit_tokens=config.limit_tokens,
                policy_version=config.policy_version,
                updated_at=config.updated_at,
                window_start=config.window_start,
                window_end=config.window_end,
                status=config.status,
                message=config.message,
            )

        key = self._user_key(user_identifier)
        summary = await self._get_hot_summary(key, config)
        config_changed = (
            summary.limit_tokens != config.limit_tokens
            or summary.policy_version != config.policy_version
            or summary.window_start != config.window_start
            or summary.window_end != config.window_end
        )
        if config_changed:
            db_summary = await self._load_user_summary(user_identifier, config)
            await self._set_hot_summary(key, db_summary)
            return self._apply_user_summary_status(db_summary, config)
        if summary.updated_at is not None or summary.used_tokens or summary.reserved_tokens:
            summary.limit_tokens = config.limit_tokens
            summary.status = config.status
            summary.message = config.message
            return self._apply_user_summary_status(summary, config)
        db_summary = await self._load_user_summary(user_identifier, config)
        await self._set_hot_summary(key, db_summary)
        return self._apply_user_summary_status(db_summary, config)

    async def _get_hot_summary(self, key: str, config: QuotaConfig) -> QuotaSummary:
        assert self._redis is not None
        values = await self._redis.hgetall(key)
        return QuotaSummary(
            used_tokens=int(values.get("used_tokens", 0) or 0),
            reserved_tokens=int(values.get("reserved_tokens", 0) or 0),
            limit_tokens=int(values.get("limit_tokens", config.limit_tokens) or config.limit_tokens),
            policy_version=int(values.get("policy_version", config.policy_version) or config.policy_version),
            updated_at=values.get("updated_at"),
            window_start=values.get("window_start") or None,
            window_end=values.get("window_end") or None,
            status=config.status,
            message=config.message,
        )

    async def _set_hot_summary(self, key: str, summary: QuotaSummary) -> None:
        assert self._redis is not None
        await self._redis.hset(
            key,
            mapping={
                "used_tokens": summary.used_tokens,
                "reserved_tokens": summary.reserved_tokens,
                "limit_tokens": summary.limit_tokens,
                "policy_version": summary.policy_version,
                "updated_at": summary.updated_at or utcnow_iso(),
                "window_start": summary.window_start or "",
                "window_end": summary.window_end or "",
            },
        )

    async def _load_thread_summary(
        self, thread_id: str, config: QuotaConfig
    ) -> QuotaSummary:
        row = await self._repository.load_thread_summary(thread_id)
        if not row:
            return QuotaSummary(
                used_tokens=0,
                reserved_tokens=0,
                limit_tokens=config.limit_tokens,
                updated_at=None,
            )
        return QuotaSummary(
            used_tokens=int(row["used_tokens"] or 0),
            reserved_tokens=int(row["reserved_tokens"] or 0),
            limit_tokens=config.limit_tokens,
            updated_at=self._serialize_datetime(row["updated_at"]),
        )

    async def _load_user_summary(
        self, user_identifier: str, config: QuotaConfig
    ) -> QuotaSummary:
        if config.status == "missing_policy":
            return QuotaSummary(
                used_tokens=0,
                reserved_tokens=0,
                limit_tokens=config.limit_tokens,
                policy_version=config.policy_version,
                updated_at=config.updated_at,
                window_start=config.window_start,
                window_end=config.window_end,
                status=config.status,
                message=config.message,
            )
        row = await self._repository.load_user_summary(user_identifier)
        matching_window = bool(
            row
            and int(row.get("policy_version") or 0) == config.policy_version
            and self._serialize_datetime(row.get("window_start")) == config.window_start
            and self._serialize_datetime(row.get("window_end")) == config.window_end
        )
        if matching_window:
            reserved_tokens = int(row["reserved_tokens"] or 0)
            return QuotaSummary(
                used_tokens=int(row["used_tokens"] or 0),
                reserved_tokens=reserved_tokens,
                limit_tokens=config.limit_tokens,
                policy_version=config.policy_version,
                updated_at=self._serialize_datetime(row["updated_at"]),
                window_start=config.window_start,
                window_end=config.window_end,
                status=config.status,
                message=config.message,
            )

        used_tokens = await self._repository.sum_user_window_usage(
            user_identifier,
            window_start=self._deserialize_datetime(config.window_start),
            window_end=self._deserialize_datetime(config.window_end),
        )
        updated_at = self._deserialize_datetime(config.updated_at) or utcnow()
        await self._repository.upsert_user_summary(
            user_identifier=user_identifier,
            used_tokens=used_tokens,
            reserved_tokens=0,
            limit_tokens=config.limit_tokens,
            policy_version=config.policy_version,
            window_start=self._deserialize_datetime(config.window_start),
            window_end=self._deserialize_datetime(config.window_end),
            updated_at=updated_at,
        )
        return QuotaSummary(
            used_tokens=used_tokens,
            reserved_tokens=0,
            limit_tokens=config.limit_tokens,
            policy_version=config.policy_version,
            updated_at=self._serialize_datetime(updated_at),
            window_start=config.window_start,
            window_end=config.window_end,
            status=config.status,
            message=config.message,
        )

    async def _persist_reservation(
        self,
        request_id: str,
        step_id: str,
        thread_id: str,
        user_identifier: str,
        reserved_tokens: int,
        prompt_tokens: int,
        thread_config: QuotaConfig,
        user_config: QuotaConfig,
    ) -> tuple[QuotaSummary, QuotaSummary]:
        now = utcnow()
        await self._repository.create_reservation(
            request_id=request_id,
            step_id=step_id,
            thread_id=thread_id,
            user_identifier=user_identifier,
            reserved_tokens=reserved_tokens,
            prompt_tokens=prompt_tokens,
            thread_limit_tokens=thread_config.limit_tokens,
            user_limit_tokens=user_config.limit_tokens,
            user_policy_version=user_config.policy_version,
            window_start=self._deserialize_datetime(user_config.window_start),
            window_end=self._deserialize_datetime(user_config.window_end),
            created_at=now,
        )

        return (
            await self._load_thread_summary(thread_id, thread_config),
            await self._load_user_summary(user_identifier, user_config),
        )

    async def _persist_settlement(
        self,
        request_id: str,
        provider_request_id: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        usage_source: str,
    ) -> tuple[QuotaSummary, QuotaSummary]:
        now = utcnow()
        row = await self._repository.settle_reservation(
            request_id=request_id,
            provider_request_id=provider_request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_source=usage_source,
            settled_at=now,
        )
        if not row:
            raise RuntimeError(f"Unknown quota request_id: {request_id}")
        thread_config = await self._config_provider.get_thread_limit(
            row["thread_id"], row["user_identifier"]
        )
        user_config = await self._config_provider.get_user_limit(
            row["user_identifier"], now
        )

        return (
            await self._load_thread_summary(row["thread_id"], thread_config),
            await self._load_user_summary(row["user_identifier"], user_config),
        )

    async def _persist_release(
        self, request_id: str, status: str
    ) -> tuple[QuotaSummary, QuotaSummary]:
        now = utcnow()
        row = await self._repository.release_reservation(
            request_id=request_id,
            status=status,
            settled_at=now,
        )
        if not row:
            raise RuntimeError(f"Unknown quota request_id: {request_id}")
        thread_config = await self._config_provider.get_thread_limit(
            row["thread_id"], row["user_identifier"]
        )
        user_config = await self._config_provider.get_user_limit(
            row["user_identifier"], now
        )

        return (
            await self._load_thread_summary(row["thread_id"], thread_config),
            await self._load_user_summary(row["user_identifier"], user_config),
        )

    async def _persist_thread_metadata(
        self, thread_id: str, thread_summary: QuotaSummary
    ) -> None:
        await self._repository.update_thread_metadata(
            thread_id=thread_id,
            token_usage_summary=thread_summary.to_metadata_summary(),
        )

    def _resolve_usage(
        self,
        prompt_estimate_tokens: int,
        assistant_content: str,
        usage: Any | None,
    ) -> tuple[int, int, int, str]:
        if usage is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            if total_tokens > 0:
                return prompt_tokens, completion_tokens, total_tokens, "litellm_usage"

        completion_tokens = self.count_text_tokens(assistant_content)
        total_tokens = prompt_estimate_tokens + completion_tokens
        return (
            prompt_estimate_tokens,
            completion_tokens,
            total_tokens,
            "fallback_tokenizer",
        )

    def _thread_key(self, thread_id: str) -> str:
        return f"quota:thread:{thread_id}"

    def _user_key(self, user_identifier: str) -> str:
        return f"quota:user:{user_identifier}"

    def _request_key(self, request_id: str) -> str:
        return f"quota:req:{request_id}"

    def _serialize_datetime(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _deserialize_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    def _is_older(self, current: str | None, other: str | None) -> bool:
        other_dt = self._deserialize_datetime(other)
        if not other_dt:
            return False
        current_dt = self._deserialize_datetime(current)
        if not current_dt:
            return True
        return current_dt < other_dt

    def _apply_user_summary_status(
        self,
        summary: QuotaSummary,
        config: QuotaConfig,
    ) -> QuotaSummary:
        summary.limit_tokens = config.limit_tokens
        summary.policy_version = config.policy_version
        summary.window_start = config.window_start
        summary.window_end = config.window_end
        summary.status = config.status
        summary.message = config.message
        if (
            config.status == "ok"
            and summary.available_tokens < self._minimum_user_request_tokens
        ):
            summary.status = "blocked"
            summary.message = self._user_blocked_message(summary)
        return summary
