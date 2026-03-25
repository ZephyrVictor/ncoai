from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

LIST_THREAD_SUMMARIES_SQL = text(
    """
    SELECT thread_id, used_tokens, reserved_tokens, limit_tokens, updated_at
    FROM quota_thread_summary
    """
)

LIST_USER_SUMMARIES_SQL = text(
    """
    SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens,
           policy_version, updated_at, window_start, window_end
    FROM quota_user_summary
    """
)

SELECT_STALE_RESERVATIONS_SQL = text(
    """
    SELECT request_id, thread_id, user_identifier, reserved_tokens
    FROM quota_usage_ledger
    WHERE status = 'reserved' AND created_at < :cutoff
    """
)

EXPIRE_LEDGER_SQL = text(
    """
    UPDATE quota_usage_ledger
    SET status = 'expired', settled_at = :now
    WHERE request_id = :request_id AND status = 'reserved'
    """
)

RELEASE_THREAD_RESERVED_SQL = text(
    """
    UPDATE quota_thread_summary
    SET reserved_tokens = GREATEST(reserved_tokens - :reserved_tokens, 0),
        updated_at = :now
    WHERE thread_id = :thread_id
    """
)

RELEASE_USER_RESERVED_SQL = text(
    """
    UPDATE quota_user_summary
    SET reserved_tokens = GREATEST(reserved_tokens - :reserved_tokens, 0),
        updated_at = :now
    WHERE user_identifier = :user_identifier
    """
)

SELECT_THREAD_SUMMARY_SQL = text(
    """
    SELECT used_tokens, reserved_tokens, limit_tokens, updated_at
    FROM quota_thread_summary
    WHERE thread_id = :thread_id
    """
)

SELECT_USER_SUMMARY_SQL = text(
    """
    SELECT used_tokens, reserved_tokens, limit_tokens, policy_version,
           updated_at, window_start, window_end
    FROM quota_user_summary
    WHERE user_identifier = :user_identifier
    """
)

SUM_USER_WINDOW_USAGE_SQL = text(
    """
    SELECT COALESCE(SUM(total_tokens), 0)::INT AS used_tokens
    FROM quota_usage_ledger
    WHERE user_identifier = :user_identifier
      AND status = 'settled'
      AND (CAST(:window_start AS TIMESTAMPTZ) IS NULL OR settled_at >= CAST(:window_start AS TIMESTAMPTZ))
      AND (CAST(:window_end AS TIMESTAMPTZ) IS NULL OR settled_at <= CAST(:window_end AS TIMESTAMPTZ))
    """
)

INSERT_USAGE_LEDGER_SQL = text(
    """
    INSERT INTO quota_usage_ledger (
        request_id,
        thread_id,
        user_identifier,
        step_id,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        reserved_tokens,
        status,
        usage_source,
        created_at,
        settled_at
    ) VALUES (
        :request_id,
        :thread_id,
        :user_identifier,
        :step_id,
        :prompt_tokens,
        0,
        0,
        :reserved_tokens,
        'reserved',
        'estimated',
        :created_at,
        NULL
    )
    """
)

UPSERT_THREAD_SUMMARY_RESERVATION_SQL = text(
    """
    INSERT INTO quota_thread_summary (
        thread_id,
        used_tokens,
        reserved_tokens,
        limit_tokens,
        updated_at
    ) VALUES (
        :thread_id,
        0,
        :reserved_tokens,
        :limit_tokens,
        :updated_at
    )
    ON CONFLICT (thread_id) DO UPDATE
    SET reserved_tokens = quota_thread_summary.reserved_tokens + EXCLUDED.reserved_tokens,
        limit_tokens = EXCLUDED.limit_tokens,
        updated_at = EXCLUDED.updated_at
    """
)

UPSERT_USER_SUMMARY_RESERVATION_SQL = text(
    """
    INSERT INTO quota_user_summary (
        user_identifier,
        used_tokens,
        reserved_tokens,
        limit_tokens,
        policy_version,
        window_start,
        window_end,
        updated_at
    ) VALUES (
        :user_identifier,
        0,
        :reserved_tokens,
        :limit_tokens,
        :policy_version,
        :window_start,
        :window_end,
        :updated_at
    )
    ON CONFLICT (user_identifier) DO UPDATE
    SET reserved_tokens = quota_user_summary.reserved_tokens + EXCLUDED.reserved_tokens,
        limit_tokens = EXCLUDED.limit_tokens,
        policy_version = EXCLUDED.policy_version,
        window_start = EXCLUDED.window_start,
        window_end = EXCLUDED.window_end,
        updated_at = EXCLUDED.updated_at
    """
)

UPSERT_USER_SUMMARY_SQL = text(
    """
    INSERT INTO quota_user_summary (
        user_identifier,
        used_tokens,
        reserved_tokens,
        limit_tokens,
        policy_version,
        window_start,
        window_end,
        updated_at
    ) VALUES (
        :user_identifier,
        :used_tokens,
        :reserved_tokens,
        :limit_tokens,
        :policy_version,
        :window_start,
        :window_end,
        :updated_at
    )
    ON CONFLICT (user_identifier) DO UPDATE
    SET used_tokens = EXCLUDED.used_tokens,
        reserved_tokens = EXCLUDED.reserved_tokens,
        limit_tokens = EXCLUDED.limit_tokens,
        policy_version = EXCLUDED.policy_version,
        window_start = EXCLUDED.window_start,
        window_end = EXCLUDED.window_end,
        updated_at = EXCLUDED.updated_at
    """
)

SELECT_LEDGER_FOR_UPDATE_SQL = text(
    """
    SELECT thread_id, user_identifier, reserved_tokens, status
    FROM quota_usage_ledger
    WHERE request_id = :request_id
    FOR UPDATE
    """
)

SETTLE_LEDGER_SQL = text(
    """
    UPDATE quota_usage_ledger
    SET provider_request_id = :provider_request_id,
        prompt_tokens = :prompt_tokens,
        completion_tokens = :completion_tokens,
        total_tokens = :total_tokens,
        usage_source = :usage_source,
        status = 'settled',
        settled_at = :settled_at
    WHERE request_id = :request_id
    """
)

SETTLE_THREAD_SUMMARY_SQL = text(
    """
    UPDATE quota_thread_summary
    SET used_tokens = used_tokens + :total_tokens,
        reserved_tokens = GREATEST(reserved_tokens - :reserved_tokens, 0),
        updated_at = :updated_at
    WHERE thread_id = :thread_id
    """
)

SETTLE_USER_SUMMARY_SQL = text(
    """
    UPDATE quota_user_summary
    SET used_tokens = used_tokens + :total_tokens,
        reserved_tokens = GREATEST(reserved_tokens - :reserved_tokens, 0),
        updated_at = :updated_at
    WHERE user_identifier = :user_identifier
    """
)

RELEASE_LEDGER_SQL = text(
    """
    UPDATE quota_usage_ledger
    SET status = :status,
        settled_at = :settled_at
    WHERE request_id = :request_id
    """
)

SELECT_THREAD_METADATA_SQL = text(
    """
    SELECT metadata
    FROM threads
    WHERE id = CAST(:thread_id AS UUID)
    FOR UPDATE
    """
)

UPDATE_THREAD_METADATA_SQL = text(
    """
    UPDATE threads
    SET metadata = CAST(:metadata AS JSONB)
    WHERE id = CAST(:thread_id AS UUID)
    """
)


class QuotaRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_thread_summaries(self) -> list[dict[str, Any]]:
        async with self._engine.begin() as conn:
            return list((await conn.execute(LIST_THREAD_SUMMARIES_SQL)).mappings())

    async def list_user_summaries(self) -> list[dict[str, Any]]:
        async with self._engine.begin() as conn:
            return list((await conn.execute(LIST_USER_SUMMARIES_SQL)).mappings())

    async def expire_stale_reservations(self, cutoff: datetime, now: datetime) -> None:
        async with self._engine.begin() as conn:
            stale_rows = list(
                (await conn.execute(SELECT_STALE_RESERVATIONS_SQL, {"cutoff": cutoff})).mappings()
            )
            for row in stale_rows:
                params = {
                    "request_id": row["request_id"],
                    "thread_id": row["thread_id"],
                    "user_identifier": row["user_identifier"],
                    "reserved_tokens": int(row["reserved_tokens"] or 0),
                    "now": now,
                }
                await conn.execute(EXPIRE_LEDGER_SQL, params)
                await conn.execute(RELEASE_THREAD_RESERVED_SQL, params)
                await conn.execute(RELEASE_USER_RESERVED_SQL, params)

    async def load_thread_summary(self, thread_id: str) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(SELECT_THREAD_SUMMARY_SQL, {"thread_id": thread_id})
            ).mappings().first()

    async def load_user_summary(self, user_identifier: str) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    SELECT_USER_SUMMARY_SQL,
                    {"user_identifier": user_identifier},
                )
            ).mappings().first()

    async def sum_user_window_usage(
        self,
        user_identifier: str,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> int:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    SUM_USER_WINDOW_USAGE_SQL,
                    {
                        "user_identifier": user_identifier,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).mappings().first()
        return int((row or {}).get("used_tokens") or 0)

    async def upsert_user_summary(
        self,
        *,
        user_identifier: str,
        used_tokens: int,
        reserved_tokens: int,
        limit_tokens: int,
        policy_version: int,
        window_start: datetime | None,
        window_end: datetime | None,
        updated_at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                UPSERT_USER_SUMMARY_SQL,
                {
                    "user_identifier": user_identifier,
                    "used_tokens": used_tokens,
                    "reserved_tokens": reserved_tokens,
                    "limit_tokens": limit_tokens,
                    "policy_version": policy_version,
                    "window_start": window_start,
                    "window_end": window_end,
                    "updated_at": updated_at,
                },
            )

    async def create_reservation(
        self,
        request_id: str,
        step_id: str,
        thread_id: str,
        user_identifier: str,
        reserved_tokens: int,
        prompt_tokens: int,
        thread_limit_tokens: int,
        user_limit_tokens: int,
        user_policy_version: int,
        window_start: str | None,
        window_end: str | None,
        created_at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                INSERT_USAGE_LEDGER_SQL,
                {
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "user_identifier": user_identifier,
                    "step_id": step_id,
                    "prompt_tokens": prompt_tokens,
                    "reserved_tokens": reserved_tokens,
                    "created_at": created_at,
                },
            )
            await conn.execute(
                UPSERT_THREAD_SUMMARY_RESERVATION_SQL,
                {
                    "thread_id": thread_id,
                    "reserved_tokens": reserved_tokens,
                    "limit_tokens": thread_limit_tokens,
                    "updated_at": created_at,
                },
            )
            await conn.execute(
                UPSERT_USER_SUMMARY_RESERVATION_SQL,
                {
                    "user_identifier": user_identifier,
                    "reserved_tokens": reserved_tokens,
                    "limit_tokens": user_limit_tokens,
                    "policy_version": user_policy_version,
                    "window_start": window_start,
                    "window_end": window_end,
                    "updated_at": created_at,
                },
            )

    async def get_request_record_for_update(self, request_id: str) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    SELECT_LEDGER_FOR_UPDATE_SQL,
                    {"request_id": request_id},
                )
            ).mappings().first()

    async def settle_reservation(
        self,
        request_id: str,
        provider_request_id: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        usage_source: str,
        settled_at: datetime,
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    SELECT_LEDGER_FOR_UPDATE_SQL,
                    {"request_id": request_id},
                )
            ).mappings().first()
            if not row:
                return None
            if row["status"] == "reserved":
                params = {
                    "request_id": request_id,
                    "provider_request_id": provider_request_id,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "usage_source": usage_source,
                    "settled_at": settled_at,
                    "thread_id": row["thread_id"],
                    "user_identifier": row["user_identifier"],
                    "reserved_tokens": int(row["reserved_tokens"] or 0),
                    "updated_at": settled_at,
                }
                await conn.execute(SETTLE_LEDGER_SQL, params)
                await conn.execute(SETTLE_THREAD_SUMMARY_SQL, params)
                await conn.execute(SETTLE_USER_SUMMARY_SQL, params)
            return dict(row)

    async def release_reservation(
        self,
        request_id: str,
        status: str,
        settled_at: datetime,
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    SELECT_LEDGER_FOR_UPDATE_SQL,
                    {"request_id": request_id},
                )
            ).mappings().first()
            if not row:
                return None
            if row["status"] == "reserved":
                params = {
                    "request_id": request_id,
                    "status": status,
                    "settled_at": settled_at,
                    "thread_id": row["thread_id"],
                    "user_identifier": row["user_identifier"],
                    "reserved_tokens": int(row["reserved_tokens"] or 0),
                    "updated_at": settled_at,
                    "now": settled_at,
                }
                await conn.execute(RELEASE_LEDGER_SQL, params)
                await conn.execute(RELEASE_THREAD_RESERVED_SQL, params)
                await conn.execute(RELEASE_USER_RESERVED_SQL, params)
            return dict(row)

    async def update_thread_metadata(
        self,
        thread_id: str,
        token_usage_summary: dict[str, Any],
    ) -> None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    SELECT_THREAD_METADATA_SQL,
                    {"thread_id": thread_id},
                )
            ).mappings().first()
            if not row:
                return
            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["token_usage_summary"] = token_usage_summary
            await conn.execute(
                UPDATE_THREAD_METADATA_SQL,
                {
                    "thread_id": thread_id,
                    "metadata": json.dumps(metadata),
                },
            )
