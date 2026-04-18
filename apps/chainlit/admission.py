from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic, time
from uuid import uuid4

from redis.asyncio import Redis

LOGGER = logging.getLogger("ncoai.chainlit.admission")

_ACQUIRE_SCRIPT = """
local holders_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local max_inflight = tonumber(ARGV[2])
local expires_at_ms = tonumber(ARGV[3])
local holder_id = ARGV[4]

redis.call("ZREMRANGEBYSCORE", holders_key, "-inf", now_ms)
local active = redis.call("ZCARD", holders_key)
if active < max_inflight then
  redis.call("ZADD", holders_key, expires_at_ms, holder_id)
  redis.call("PEXPIRE", holders_key, math.max(expires_at_ms - now_ms, 1000))
  return {1, active + 1}
end

return {0, active}
"""

_RENEW_SCRIPT = """
local holders_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local expires_at_ms = tonumber(ARGV[2])
local holder_id = ARGV[3]

redis.call("ZREMRANGEBYSCORE", holders_key, "-inf", now_ms)
if redis.call("ZSCORE", holders_key, holder_id) then
  redis.call("ZADD", holders_key, expires_at_ms, holder_id)
  redis.call("PEXPIRE", holders_key, math.max(expires_at_ms - now_ms, 1000))
  return 1
end

return 0
"""

_RELEASE_SCRIPT = """
local holders_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local holder_id = ARGV[2]

redis.call("ZREMRANGEBYSCORE", holders_key, "-inf", now_ms)
return redis.call("ZREM", holders_key, holder_id)
"""

_COUNT_SCRIPT = """
local holders_key = KEYS[1]
local now_ms = tonumber(ARGV[1])

redis.call("ZREMRANGEBYSCORE", holders_key, "-inf", now_ms)
return redis.call("ZCARD", holders_key)
"""


@dataclass(slots=True)
class AdmissionLease:
    holder_id: str
    acquired_at: float
    _renew_task: asyncio.Task[None] | None = field(default=None, repr=False)


class GlobalAdmissionController:
    def __init__(
        self,
        *,
        redis_url: str,
        max_inflight: int,
        queue_timeout_seconds: float,
        worker_id: str,
        key_prefix: str = "chainlit:global-admission:litellm",
        lease_ttl_seconds: float = 30.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self._redis_url = redis_url
        self._max_inflight = max(max_inflight, 1)
        self._queue_timeout_seconds = max(queue_timeout_seconds, 0.1)
        self._worker_id = worker_id
        self._key = f"{key_prefix}:holders"
        self._lease_ttl_seconds = max(lease_ttl_seconds, 5.0)
        self._poll_interval_seconds = max(poll_interval_seconds, 0.01)
        self._redis: Redis | None = None

    async def initialize(self) -> None:
        if self._redis is None:
            self._redis = Redis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    @property
    def queue_timeout_seconds(self) -> float:
        return self._queue_timeout_seconds

    @property
    def max_inflight(self) -> int:
        return self._max_inflight

    async def is_saturated(self) -> bool:
        return await self.current_inflight() >= self._max_inflight

    async def current_inflight(self) -> int:
        await self.initialize()
        assert self._redis is not None
        result = await self._redis.eval(
            _COUNT_SCRIPT,
            1,
            self._key,
            self._now_ms(),
        )
        return int(result or 0)

    async def acquire(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[AdmissionLease | None, float]:
        await self.initialize()
        assert self._redis is not None

        holder_id = f"{self._worker_id}:{uuid4()}"
        effective_timeout = (
            self._queue_timeout_seconds
            if timeout_seconds is None
            else max(timeout_seconds, 0.1)
        )
        started_at = monotonic()
        deadline = started_at + effective_timeout

        while True:
            now_ms = self._now_ms()
            expires_at_ms = now_ms + int(self._lease_ttl_seconds * 1000)
            result = await self._redis.eval(
                _ACQUIRE_SCRIPT,
                1,
                self._key,
                now_ms,
                self._max_inflight,
                expires_at_ms,
                holder_id,
            )
            if result and int(result[0]) == 1:
                lease = AdmissionLease(
                    holder_id=holder_id,
                    acquired_at=time(),
                )
                lease._renew_task = asyncio.create_task(
                    self._heartbeat_loop(holder_id)
                )
                return lease, max(monotonic() - started_at, 0.0)

            remaining = deadline - monotonic()
            if remaining <= 0:
                return None, max(monotonic() - started_at, 0.0)
            await asyncio.sleep(min(self._poll_interval_seconds, remaining))

    async def release(self, lease: AdmissionLease | None) -> None:
        if lease is None:
            return

        renew_task = lease._renew_task
        if renew_task is not None:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass

        if self._redis is None:
            return

        try:
            await self._redis.eval(
                _RELEASE_SCRIPT,
                1,
                self._key,
                self._now_ms(),
                lease.holder_id,
            )
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning("Failed to release global admission lease: %s", exc)

    async def _heartbeat_loop(self, holder_id: str) -> None:
        if self._redis is None:
            return

        sleep_seconds = max(self._lease_ttl_seconds / 3, 1.0)
        try:
            while True:
                await asyncio.sleep(sleep_seconds)
                if self._redis is None:
                    return
                now_ms = self._now_ms()
                expires_at_ms = now_ms + int(self._lease_ttl_seconds * 1000)
                renewed = await self._redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    self._key,
                    now_ms,
                    expires_at_ms,
                    holder_id,
                )
                if int(renewed or 0) != 1:
                    LOGGER.warning(
                        "Global admission lease %s expired before renewal",
                        holder_id,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning("Global admission heartbeat failed: %s", exc)

    @staticmethod
    def _now_ms() -> int:
        return int(time() * 1000)
