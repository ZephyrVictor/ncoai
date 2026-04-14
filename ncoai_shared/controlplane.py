from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ncoai_shared.team_token_auth import TEAM_TOKEN_AUTH_CONFIG_KEY


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


@dataclass(slots=True)
class EffectiveQuotaPolicy:
    id: str | None
    scope_type: str
    scope_id: str | None
    window_start: str | None
    window_end: str | None
    total_token_limit: int
    is_active: bool
    policy_version: int


TEAM_TOKEN_IDENTIFIER_PREFIX = "gzctf-team:"
TEAM_TOKEN_AUTH_PASSWORD_HASH = "!team-token-auth!"


GET_ADMIN_ACCOUNT_SQL = text(
    """
    SELECT username, password_hash, is_active, created_at, updated_at
    FROM admin_accounts
    WHERE username = :username
    """
)

UPSERT_ADMIN_ACCOUNT_SQL = text(
    """
    INSERT INTO admin_accounts (
        username,
        password_hash,
        is_active,
        created_at,
        updated_at
    ) VALUES (
        :username,
        :password_hash,
        TRUE,
        :now,
        :now
    )
    ON CONFLICT (username) DO UPDATE
    SET password_hash = EXCLUDED.password_hash,
        is_active = TRUE,
        updated_at = EXCLUDED.updated_at
    """
)

GET_MANAGED_USER_SQL = text(
    """
    SELECT identifier, display_name, password_hash, is_active, is_deleted,
           created_at, updated_at, deleted_at, last_login_at
    FROM managed_users
    WHERE identifier = :identifier
      AND (:include_deleted OR is_deleted = FALSE)
    """
)

LIST_MANAGED_USERS_SQL = text(
    """
    SELECT identifier, display_name, password_hash, is_active, is_deleted,
           created_at, updated_at, deleted_at, last_login_at
    FROM managed_users
    WHERE (:include_deleted OR is_deleted = FALSE)
    ORDER BY created_at DESC, identifier ASC
    """
)

UPSERT_MANAGED_USER_SQL = text(
    """
    INSERT INTO managed_users (
        identifier,
        display_name,
        password_hash,
        is_active,
        is_deleted,
        created_at,
        updated_at,
        deleted_at,
        last_login_at
    ) VALUES (
        :identifier,
        :display_name,
        :password_hash,
        :is_active,
        FALSE,
        :created_at,
        :updated_at,
        NULL,
        NULL
    )
    ON CONFLICT (identifier) DO UPDATE
    SET display_name = EXCLUDED.display_name,
        password_hash = EXCLUDED.password_hash,
        is_active = EXCLUDED.is_active,
        is_deleted = FALSE,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at
    """
)

UPDATE_MANAGED_USER_SQL = text(
    """
    UPDATE managed_users
    SET display_name = :display_name,
        password_hash = COALESCE(:password_hash, password_hash),
        is_active = :is_active,
        is_deleted = FALSE,
        deleted_at = NULL,
        updated_at = :updated_at
    WHERE identifier = :identifier
    """
)

SOFT_DELETE_MANAGED_USER_SQL = text(
    """
    UPDATE managed_users
    SET is_active = FALSE,
        is_deleted = TRUE,
        deleted_at = :deleted_at,
        updated_at = :updated_at
    WHERE identifier = :identifier
      AND is_deleted = FALSE
    """
)

TOUCH_MANAGED_USER_LOGIN_SQL = text(
    """
    UPDATE managed_users
    SET last_login_at = :last_login_at,
        updated_at = :updated_at
    WHERE identifier = :identifier
    """
)

LIST_USER_USAGE_SUMMARIES_SQL = text(
    """
    SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens,
           policy_version, window_start, window_end, updated_at
    FROM quota_user_summary
    """
)

LIST_TEAM_TOKEN_CHAT_USERS_SQL = text(
    """
    SELECT
        identifier,
        COALESCE(
            NULLIF(metadata ->> 'display_name', ''),
            CASE
                WHEN NULLIF(metadata ->> 'team_id', '') IS NOT NULL
                THEN 'Team #' || (metadata ->> 'team_id')
                ELSE identifier
            END
        ) AS display_name
    FROM users
    WHERE identifier LIKE :identifier_prefix
       OR COALESCE(metadata ->> 'auth_source', '') = 'team-token'
    ORDER BY identifier ASC
    """
)

LIST_THREAD_ACTIVITY_SQL = text(
    """
    WITH thread_activity AS (
        SELECT
            t."userIdentifier" AS identifier,
            t.id AS thread_id,
            MAX(
                COALESCE(
                    NULLIF(s."createdAt", '')::timestamptz,
                    NULLIF(t."createdAt", '')::timestamptz
                )
            ) AS last_active_at
        FROM threads t
        LEFT JOIN steps s ON s."threadId" = t.id
        WHERE t."userIdentifier" IS NOT NULL
        GROUP BY t."userIdentifier", t.id
    )
    SELECT
        identifier,
        COUNT(*)::INT AS thread_count_total,
        COUNT(*) FILTER (WHERE last_active_at >= :cutoff)::INT AS active_threads_24h,
        MAX(last_active_at) AS last_active_at
    FROM thread_activity
    GROUP BY identifier
    """
)

AGGREGATE_WINDOW_USAGE_SQL = text(
    """
    SELECT
        user_identifier,
        COALESCE(SUM(total_tokens), 0)::INT AS used_tokens
    FROM quota_usage_ledger
    WHERE status = 'settled'
      AND (CAST(:user_identifier AS TEXT) IS NULL OR user_identifier = CAST(:user_identifier AS TEXT))
      AND (CAST(:window_start AS TIMESTAMPTZ) IS NULL OR settled_at >= CAST(:window_start AS TIMESTAMPTZ))
      AND (CAST(:window_end AS TIMESTAMPTZ) IS NULL OR settled_at <= CAST(:window_end AS TIMESTAMPTZ))
    GROUP BY user_identifier
    """
)

LIST_QUOTA_POLICIES_SQL = text(
    """
    SELECT id, scope_type, scope_id, window_start, window_end,
           total_token_limit, is_active, policy_version, created_at, updated_at
    FROM quota_policies
    ORDER BY
        CASE scope_type WHEN 'global' THEN 0 ELSE 1 END,
        updated_at DESC,
        id DESC
    """
)

GET_LATEST_GLOBAL_QUOTA_POLICY_SQL = text(
    """
    SELECT id, scope_type, scope_id, window_start, window_end,
           total_token_limit, is_active, policy_version, created_at, updated_at
    FROM quota_policies
    WHERE scope_type = 'global'
    ORDER BY updated_at DESC, created_at DESC
    LIMIT 1
    """
)

DELETE_OTHER_GLOBAL_POLICIES_SQL = text(
    """
    DELETE FROM quota_policies
    WHERE scope_type = 'global'
      AND id <> :keep_id
    """
)

DELETE_NON_GLOBAL_POLICIES_SQL = text(
    """
    DELETE FROM quota_policies
    WHERE scope_type <> 'global'
    """
)

GET_QUOTA_POLICY_SQL = text(
    """
    SELECT id, scope_type, scope_id, window_start, window_end,
           total_token_limit, is_active, policy_version, created_at, updated_at
    FROM quota_policies
    WHERE id = :policy_id
    """
)

INSERT_QUOTA_POLICY_SQL = text(
    """
    INSERT INTO quota_policies (
        id,
        scope_type,
        scope_id,
        window_start,
        window_end,
        total_token_limit,
        is_active,
        policy_version,
        created_at,
        updated_at
    ) VALUES (
        :id,
        :scope_type,
        :scope_id,
        :window_start,
        :window_end,
        :total_token_limit,
        :is_active,
        :policy_version,
        :created_at,
        :updated_at
    )
    """
)

UPDATE_QUOTA_POLICY_SQL = text(
    """
    UPDATE quota_policies
    SET scope_type = :scope_type,
        scope_id = :scope_id,
        window_start = :window_start,
        window_end = :window_end,
        total_token_limit = :total_token_limit,
        is_active = :is_active,
        policy_version = :policy_version,
        updated_at = :updated_at
    WHERE id = :id
    """
)

DELETE_QUOTA_POLICY_SQL = text(
    """
    DELETE FROM quota_policies
    WHERE id = :policy_id
    """
)

GET_EFFECTIVE_POLICY_SQL = text(
    """
    SELECT id, scope_type, scope_id, window_start, window_end,
           total_token_limit, is_active, policy_version, created_at, updated_at
    FROM quota_policies
    WHERE is_active = TRUE
      AND (
        (scope_type = 'user' AND scope_id = :user_identifier)
        OR scope_type = 'global'
      )
      AND (window_start IS NULL OR window_start <= :now)
      AND (window_end IS NULL OR window_end >= :now)
    ORDER BY
      CASE scope_type WHEN 'user' THEN 0 ELSE 1 END,
      updated_at DESC,
      created_at DESC
    LIMIT 1
    """
)

GET_LATEST_USER_OVERRIDE_SQL = text(
    """
    SELECT id, scope_type, scope_id, window_start, window_end,
           total_token_limit, is_active, policy_version, created_at, updated_at
    FROM quota_policies
    WHERE scope_type = 'user'
      AND scope_id = :user_identifier
    ORDER BY updated_at DESC, created_at DESC
    LIMIT 1
    """
)

DELETE_USER_OVERRIDE_POLICIES_SQL = text(
    """
    DELETE FROM quota_policies
    WHERE scope_type = 'user'
      AND scope_id = :user_identifier
    """
)

INSERT_IMPORT_JOB_SQL = text(
    """
    INSERT INTO user_import_jobs (
        id,
        preview_only,
        status,
        total_rows,
        success_rows,
        failure_rows,
        created_at,
        updated_at
    ) VALUES (
        :id,
        :preview_only,
        :status,
        :total_rows,
        :success_rows,
        :failure_rows,
        :created_at,
        :updated_at
    )
    """
)

DELETE_IMPORT_JOB_ROWS_SQL = text(
    """
    DELETE FROM user_import_job_rows
    WHERE job_id = :job_id
    """
)

INSERT_IMPORT_JOB_ROW_SQL = text(
    """
    INSERT INTO user_import_job_rows (
        id,
        job_id,
        row_number,
        identifier,
        action,
        status,
        error_message,
        row_payload,
        created_at
    ) VALUES (
        :id,
        :job_id,
        :row_number,
        :identifier,
        :action,
        :status,
        :error_message,
        CAST(:row_payload AS JSONB),
        :created_at
    )
    """
)

UPDATE_IMPORT_JOB_SQL = text(
    """
    UPDATE user_import_jobs
    SET status = :status,
        success_rows = :success_rows,
        failure_rows = :failure_rows,
        updated_at = :updated_at
    WHERE id = :id
    """
)

UPSERT_USER_WINDOW_SUMMARY_SQL = text(
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

GET_TEAM_TOKEN_AUTH_CONFIG_SQL = text(
    """
    SELECT config_key, public_key, config_version, created_at, updated_at
    FROM team_token_auth_config
    WHERE config_key = :config_key
    """
)

INSERT_TEAM_TOKEN_AUTH_CONFIG_SQL = text(
    """
    INSERT INTO team_token_auth_config (
        config_key,
        public_key,
        config_version,
        created_at,
        updated_at
    ) VALUES (
        :config_key,
        :public_key,
        :config_version,
        :created_at,
        :updated_at
    )
    """
)

UPDATE_TEAM_TOKEN_AUTH_CONFIG_SQL = text(
    """
    UPDATE team_token_auth_config
    SET public_key = :public_key,
        config_version = :config_version,
        updated_at = :updated_at
    WHERE config_key = :config_key
    """
)

INSERT_CAPS_SUBMISSION_SQL = text(
    """
    INSERT INTO caps_submissions (
        id,
        user_identifier,
        game_id,
        team_id,
        player_name,
        original_filename,
        stored_filename,
        relative_path,
        file_extension,
        content_type,
        file_size_bytes,
        created_at
    ) VALUES (
        :id,
        :user_identifier,
        :game_id,
        :team_id,
        :player_name,
        :original_filename,
        :stored_filename,
        :relative_path,
        :file_extension,
        :content_type,
        :file_size_bytes,
        :created_at
    )
    """
)


class ControlPlaneRepository:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        minimum_available_tokens_for_request: int = 0,
    ) -> None:
        self._engine = engine
        self._minimum_available_tokens_for_request = (
            minimum_available_tokens_for_request
        )

    async def bootstrap_admin_account(
        self,
        username: str,
        password_hash: str,
    ) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                UPSERT_ADMIN_ACCOUNT_SQL,
                {
                    "username": username,
                    "password_hash": password_hash,
                    "now": now,
                },
            )

    async def bootstrap_managed_user(
        self,
        identifier: str,
        password_hash: str,
        display_name: str,
        *,
        is_active: bool = True,
    ) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                UPSERT_MANAGED_USER_SQL,
                {
                    "identifier": identifier,
                    "display_name": display_name,
                    "password_hash": password_hash,
                    "is_active": is_active,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    async def sync_team_token_subjects(self) -> int:
        created = 0
        now = utcnow()
        async with self._engine.begin() as conn:
            team_rows = list(
                (
                    await conn.execute(
                        LIST_TEAM_TOKEN_CHAT_USERS_SQL,
                        {
                            "identifier_prefix": f"{TEAM_TOKEN_IDENTIFIER_PREFIX}%",
                        },
                    )
                ).mappings()
            )
            existing_identifiers = {
                row["identifier"]
                for row in (
                    await conn.execute(
                        LIST_MANAGED_USERS_SQL,
                        {"include_deleted": True},
                    )
                ).mappings()
            }

            for row in team_rows:
                identifier = str(row["identifier"])
                if identifier in existing_identifiers:
                    continue

                await conn.execute(
                    UPSERT_MANAGED_USER_SQL,
                    {
                        "identifier": identifier,
                        "display_name": self._subject_display_name(
                            identifier,
                            row.get("display_name"),
                        ),
                        "password_hash": TEAM_TOKEN_AUTH_PASSWORD_HASH,
                        "is_active": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                existing_identifiers.add(identifier)
                created += 1

        return created

    async def bootstrap_global_quota_policy(
        self,
        total_token_limit: int,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT id
                        FROM quota_policies
                        WHERE scope_type = 'global'
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """
                    )
                )
            ).scalar_one_or_none()
            if existing:
                await conn.execute(
                    DELETE_OTHER_GLOBAL_POLICIES_SQL,
                    {"keep_id": existing},
                )
                await conn.execute(DELETE_NON_GLOBAL_POLICIES_SQL)
                return
            await conn.execute(
                INSERT_QUOTA_POLICY_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "scope_type": "global",
                    "scope_id": None,
                    "window_start": window_start,
                    "window_end": window_end,
                    "total_token_limit": total_token_limit,
                    "is_active": True,
                    "policy_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await conn.execute(DELETE_NON_GLOBAL_POLICIES_SQL)

    async def get_team_token_auth_config(self) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_TEAM_TOKEN_AUTH_CONFIG_SQL,
                    {"config_key": TEAM_TOKEN_AUTH_CONFIG_KEY},
                )
            ).mappings().first()
        return self._serialize_team_token_auth_config(row)

    async def bootstrap_team_token_auth_config(
        self,
        public_key: str,
    ) -> dict[str, Any]:
        normalized_public_key = str(public_key).strip()
        if not normalized_public_key:
            raise ValueError("public_key is required")
        now = utcnow()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_TEAM_TOKEN_AUTH_CONFIG_SQL,
                    {"config_key": TEAM_TOKEN_AUTH_CONFIG_KEY},
                )
            ).mappings().first()
            if row:
                serialized = self._serialize_team_token_auth_config(row)
                if serialized is None:
                    raise RuntimeError("Team token auth config bootstrap failed")
                return serialized
            await conn.execute(
                INSERT_TEAM_TOKEN_AUTH_CONFIG_SQL,
                {
                    "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
                    "public_key": normalized_public_key,
                    "config_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        config = await self.get_team_token_auth_config()
        if config is None:
            raise RuntimeError("Team token auth config bootstrap failed")
        return config

    async def upsert_team_token_auth_config(
        self,
        *,
        public_key: str,
    ) -> tuple[dict[str, Any], bool]:
        normalized_public_key = str(public_key).strip()
        if not normalized_public_key:
            raise ValueError("public_key is required")
        changed = False
        now = utcnow()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_TEAM_TOKEN_AUTH_CONFIG_SQL,
                    {"config_key": TEAM_TOKEN_AUTH_CONFIG_KEY},
                )
            ).mappings().first()
            if row:
                current_public_key = str(row.get("public_key") or "").strip()
                next_version = int(row.get("config_version") or 0)
                if current_public_key != normalized_public_key:
                    next_version += 1
                    changed = True
                    await conn.execute(
                        UPDATE_TEAM_TOKEN_AUTH_CONFIG_SQL,
                        {
                            "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
                            "public_key": normalized_public_key,
                            "config_version": next_version,
                            "updated_at": now,
                        },
                    )
            else:
                changed = True
                await conn.execute(
                    INSERT_TEAM_TOKEN_AUTH_CONFIG_SQL,
                    {
                        "config_key": TEAM_TOKEN_AUTH_CONFIG_KEY,
                        "public_key": normalized_public_key,
                        "config_version": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
        config = await self.get_team_token_auth_config()
        if config is None:
            raise RuntimeError("Team token auth config update failed")
        return config, changed

    async def create_caps_submission(
        self,
        *,
        user_identifier: str,
        game_id: int,
        team_id: int,
        player_name: str,
        original_filename: str,
        stored_filename: str,
        relative_path: str,
        file_extension: str,
        content_type: str | None,
        file_size_bytes: int,
    ) -> dict[str, Any]:
        submission_id = str(uuid.uuid4())
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                INSERT_CAPS_SUBMISSION_SQL,
                {
                    "id": submission_id,
                    "user_identifier": user_identifier,
                    "game_id": game_id,
                    "team_id": team_id,
                    "player_name": player_name,
                    "original_filename": original_filename,
                    "stored_filename": stored_filename,
                    "relative_path": relative_path,
                    "file_extension": file_extension,
                    "content_type": content_type,
                    "file_size_bytes": file_size_bytes,
                    "created_at": now,
                },
            )
        return {
            "id": submission_id,
            "user_identifier": user_identifier,
            "game_id": game_id,
            "team_id": team_id,
            "player_name": player_name,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "relative_path": relative_path,
            "file_extension": file_extension,
            "content_type": content_type,
            "file_size_bytes": file_size_bytes,
            "created_at": serialize_datetime(now),
        }

    async def get_admin_account(self, username: str) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(GET_ADMIN_ACCOUNT_SQL, {"username": username})
            ).mappings().first()
        return dict(row) if row else None

    async def get_managed_user(
        self,
        identifier: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_MANAGED_USER_SQL,
                    {
                        "identifier": identifier,
                        "include_deleted": include_deleted,
                    },
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_managed_users(
        self,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        now = utcnow()
        cutoff = now.replace(microsecond=0) - timedelta(hours=24)
        async with self._engine.begin() as conn:
            user_rows = list(
                (
                    await conn.execute(
                        LIST_MANAGED_USERS_SQL,
                        {"include_deleted": include_deleted},
                    )
                ).mappings()
            )
            activity_rows = list(
                (
                    await conn.execute(
                        LIST_THREAD_ACTIVITY_SQL,
                        {"cutoff": cutoff},
                    )
                ).mappings()
            )
            summary_rows = list(
                (
                    await conn.execute(LIST_USER_USAGE_SUMMARIES_SQL)
                ).mappings()
            )
            global_policy_row = (
                await conn.execute(GET_LATEST_GLOBAL_QUOTA_POLICY_SQL)
            ).mappings().first()
        summary_by_user = {
            row["user_identifier"]: dict(row)
            for row in summary_rows
        }
        activity_by_user = {
            row["identifier"]: dict(row)
            for row in activity_rows
        }

        global_policy = dict(global_policy_row) if global_policy_row else None
        policy_limit_tokens = (
            int(global_policy["total_token_limit"])
            if global_policy and global_policy.get("total_token_limit") is not None
            else None
        )
        global_policy_window_open = bool(
            global_policy_row and self._policy_is_active_now(global_policy_row, now)
        )

        result: list[dict[str, Any]] = []
        for row in user_rows:
            identifier = row["identifier"]
            usage = await self._resolve_user_usage_summary(
                identifier=identifier,
                global_policy=global_policy,
                summary_row=summary_by_user.get(identifier),
            )
            activity = activity_by_user.get(identifier, {})
            used_tokens = int(usage.get("used_tokens") or 0)
            reserved_tokens = int(usage.get("reserved_tokens") or 0)
            remaining_tokens = (
                max(policy_limit_tokens - used_tokens, 0)
                if policy_limit_tokens is not None
                else None
            )
            quota_state = "missing_policy"
            if row["is_deleted"]:
                quota_state = "deleted"
            elif not row["is_active"]:
                quota_state = "inactive"
            elif not global_policy or not global_policy.get("is_active"):
                quota_state = "missing_policy"
            elif not global_policy_window_open:
                quota_state = "window_closed"
            elif (
                policy_limit_tokens is not None
                and remaining_tokens is not None
                and remaining_tokens < self._minimum_available_tokens_for_request
            ):
                quota_state = "blocked"
            elif policy_limit_tokens and used_tokens >= int(policy_limit_tokens * 0.9):
                quota_state = "near_limit"
            else:
                quota_state = "ok"

            result.append(
                self._decorate_subject_row(
                    {
                        "identifier": identifier,
                        "display_name": row["display_name"],
                        "is_active": bool(row["is_active"]),
                        "is_deleted": bool(row["is_deleted"]),
                        "created_at": serialize_datetime(row["created_at"]),
                        "updated_at": serialize_datetime(row["updated_at"]),
                        "deleted_at": serialize_datetime(row["deleted_at"]),
                        "last_login_at": serialize_datetime(row["last_login_at"]),
                        "last_active_at": serialize_datetime(activity.get("last_active_at")),
                        "thread_count_total": int(activity.get("thread_count_total") or 0),
                        "active_threads_24h": int(activity.get("active_threads_24h") or 0),
                        "used_tokens": used_tokens,
                        "reserved_tokens": reserved_tokens,
                        "current_limit_tokens": policy_limit_tokens,
                        "remaining_tokens": remaining_tokens,
                        "quota_state": quota_state,
                        "effective_policy": self._serialize_policy(global_policy),
                    }
                )
            )
        return result

    async def create_managed_user(
        self,
        identifier: str,
        display_name: str,
        password_hash: str,
        is_active: bool,
    ) -> None:
        await self.bootstrap_managed_user(
            identifier=identifier,
            password_hash=password_hash,
            display_name=display_name,
            is_active=is_active,
        )

    async def update_managed_user(
        self,
        identifier: str,
        *,
        display_name: str,
        password_hash: str | None,
        is_active: bool,
    ) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                UPDATE_MANAGED_USER_SQL,
                {
                    "identifier": identifier,
                    "display_name": display_name,
                    "password_hash": password_hash,
                    "is_active": is_active,
                    "updated_at": now,
                },
            )

    async def soft_delete_managed_user(self, identifier: str) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                SOFT_DELETE_MANAGED_USER_SQL,
                {
                    "identifier": identifier,
                    "deleted_at": now,
                    "updated_at": now,
                },
            )

    async def touch_managed_user_login(self, identifier: str) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                TOUCH_MANAGED_USER_LOGIN_SQL,
                {
                    "identifier": identifier,
                    "last_login_at": now,
                    "updated_at": now,
                },
            )

    async def list_quota_policies(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or utcnow()
        async with self._engine.begin() as conn:
            rows = list(
                (
                    await conn.execute(
                        text(
                            """
                            SELECT id, scope_type, scope_id, window_start, window_end,
                                   total_token_limit, is_active, policy_version, created_at, updated_at
                            FROM quota_policies
                            WHERE scope_type = 'global'
                            ORDER BY updated_at DESC, created_at DESC
                            """
                        )
                    )
                ).mappings()
            )
        result = []
        for row in rows:
            policy = self._serialize_policy(dict(row)) or {}
            policy["active_now"] = self._policy_is_active_now(row, current)
            result.append(policy)
        return result

    async def get_global_quota_policy(self) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(GET_LATEST_GLOBAL_QUOTA_POLICY_SQL)
            ).mappings().first()
        if not row:
            return None
        return self._serialize_policy(dict(row))

    async def upsert_global_quota_policy(
        self,
        *,
        total_token_limit: int,
        window_start: datetime | None,
        window_end: datetime | None,
        is_active: bool,
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(GET_LATEST_GLOBAL_QUOTA_POLICY_SQL)
            ).mappings().first()
            policy_id = row["id"] if row else str(uuid.uuid4())
            policy_version = int(row["policy_version"] or 0) + 1 if row else 1
            if row:
                await conn.execute(
                    UPDATE_QUOTA_POLICY_SQL,
                    {
                        "id": policy_id,
                        "scope_type": "global",
                        "scope_id": None,
                        "window_start": window_start,
                        "window_end": window_end,
                        "total_token_limit": total_token_limit,
                        "is_active": is_active,
                        "policy_version": policy_version,
                        "updated_at": now,
                    },
                )
            else:
                await conn.execute(
                    INSERT_QUOTA_POLICY_SQL,
                    {
                        "id": policy_id,
                        "scope_type": "global",
                        "scope_id": None,
                        "window_start": window_start,
                        "window_end": window_end,
                        "total_token_limit": total_token_limit,
                        "is_active": is_active,
                        "policy_version": policy_version,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            await conn.execute(
                DELETE_OTHER_GLOBAL_POLICIES_SQL,
                {"keep_id": policy_id},
            )
            await conn.execute(DELETE_NON_GLOBAL_POLICIES_SQL)
        policy = await self.get_global_quota_policy()
        if policy is None:
            raise RuntimeError("Global quota policy update failed")
        return policy

    async def get_quota_policy(self, policy_id: str) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_QUOTA_POLICY_SQL,
                    {"policy_id": policy_id},
                )
            ).mappings().first()
        if not row:
            return None
        policy = dict(row)
        policy["window_start"] = serialize_datetime(policy.get("window_start"))
        policy["window_end"] = serialize_datetime(policy.get("window_end"))
        policy["created_at"] = serialize_datetime(policy.get("created_at"))
        policy["updated_at"] = serialize_datetime(policy.get("updated_at"))
        return policy

    async def create_quota_policy(
        self,
        *,
        scope_type: str,
        scope_id: str | None,
        window_start: datetime | None,
        window_end: datetime | None,
        total_token_limit: int,
        is_active: bool,
    ) -> str:
        now = utcnow()
        policy_id = str(uuid.uuid4())
        async with self._engine.begin() as conn:
            await conn.execute(
                INSERT_QUOTA_POLICY_SQL,
                {
                    "id": policy_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "total_token_limit": total_token_limit,
                    "is_active": is_active,
                    "policy_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return policy_id

    async def update_quota_policy(
        self,
        policy_id: str,
        *,
        scope_type: str,
        scope_id: str | None,
        window_start: datetime | None,
        window_end: datetime | None,
        total_token_limit: int,
        is_active: bool,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                UPDATE_QUOTA_POLICY_SQL,
                {
                    "id": policy_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "total_token_limit": total_token_limit,
                    "is_active": is_active,
                    "policy_version": 1,
                    "updated_at": utcnow(),
                },
            )

    async def delete_quota_policy(self, policy_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                DELETE_QUOTA_POLICY_SQL,
                {"policy_id": policy_id},
            )

    async def get_effective_policy(
        self,
        user_identifier: str,
        now: datetime | None = None,
    ) -> EffectiveQuotaPolicy | None:
        del user_identifier
        current = now or utcnow()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(GET_LATEST_GLOBAL_QUOTA_POLICY_SQL)
            ).mappings().first()
        if not row:
            return None
        if not row["is_active"] or not self._policy_is_active_now(row, current):
            return None
        return EffectiveQuotaPolicy(
            id=row["id"],
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            window_start=serialize_datetime(row["window_start"]),
            window_end=serialize_datetime(row["window_end"]),
            total_token_limit=int(row["total_token_limit"]),
            is_active=bool(row["is_active"]),
            policy_version=int(row["policy_version"] or 0),
        )

    async def get_latest_user_override_policy(
        self,
        user_identifier: str,
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    GET_LATEST_USER_OVERRIDE_SQL,
                    {"user_identifier": user_identifier},
                )
            ).mappings().first()
        if not row:
            return None
        policy = dict(row)
        policy["window_start"] = serialize_datetime(policy.get("window_start"))
        policy["window_end"] = serialize_datetime(policy.get("window_end"))
        policy["created_at"] = serialize_datetime(policy.get("created_at"))
        policy["updated_at"] = serialize_datetime(policy.get("updated_at"))
        return policy

    async def replace_user_override_policy(
        self,
        user_identifier: str,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        total_token_limit: int,
        is_active: bool,
    ) -> str:
        async with self._engine.begin() as conn:
            await conn.execute(
                DELETE_USER_OVERRIDE_POLICIES_SQL,
                {"user_identifier": user_identifier},
            )
        return await self.create_quota_policy(
            scope_type="user",
            scope_id=user_identifier,
            window_start=window_start,
            window_end=window_end,
            total_token_limit=total_token_limit,
            is_active=is_active,
        )

    async def delete_user_override_policies(self, user_identifier: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                DELETE_USER_OVERRIDE_POLICIES_SQL,
                {"user_identifier": user_identifier},
            )

    async def get_user_usage(self, user_identifier: str) -> dict[str, Any]:
        users = await self.list_managed_users(include_deleted=True)
        for row in users:
            if row["identifier"] == user_identifier:
                return row
        raise KeyError(user_identifier)

    def _decorate_subject_row(self, row: dict[str, Any]) -> dict[str, Any]:
        identifier = str(row["identifier"])
        auth_source = (
            "team-token"
            if identifier.startswith(TEAM_TOKEN_IDENTIFIER_PREFIX)
            else "managed-user"
        )
        next_row = dict(row)
        next_row["display_name"] = self._subject_display_name(
            identifier,
            row.get("display_name"),
        )
        next_row["auth_source"] = auth_source
        next_row["auth_label"] = "Team Token" if auth_source == "team-token" else "密码"
        next_row["is_password_editable"] = auth_source != "team-token"
        return next_row

    def _subject_display_name(
        self,
        identifier: str,
        display_name: Any,
    ) -> str:
        normalized = str(display_name or "").strip()
        if normalized:
            return normalized
        if identifier.startswith(TEAM_TOKEN_IDENTIFIER_PREFIX):
            parts = identifier.split(":")
            if len(parts) >= 3 and parts[-1].isdigit():
                return f"Team #{parts[-1]}"
        return identifier

    async def create_import_job(
        self,
        *,
        preview_only: bool,
        total_rows: int,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                INSERT_IMPORT_JOB_SQL,
                {
                    "id": job_id,
                    "preview_only": preview_only,
                    "status": "preview" if preview_only else "processing",
                    "total_rows": total_rows,
                    "success_rows": 0,
                    "failure_rows": 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return job_id

    async def replace_import_job_rows(
        self,
        job_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(DELETE_IMPORT_JOB_ROWS_SQL, {"job_id": job_id})
            for row in rows:
                await conn.execute(
                    INSERT_IMPORT_JOB_ROW_SQL,
                    {
                        "id": str(uuid.uuid4()),
                        "job_id": job_id,
                        "row_number": row["row_number"],
                        "identifier": row.get("identifier"),
                        "action": row.get("action"),
                        "status": row.get("status"),
                        "error_message": row.get("error_message"),
                        "row_payload": json.dumps(row.get("row_payload") or {}),
                        "created_at": now,
                    },
                )

    async def complete_import_job(
        self,
        job_id: str,
        *,
        status: str,
        success_rows: int,
        failure_rows: int,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                UPDATE_IMPORT_JOB_SQL,
                {
                    "id": job_id,
                    "status": status,
                    "success_rows": success_rows,
                    "failure_rows": failure_rows,
                    "updated_at": utcnow(),
                },
            )

    def _serialize_policy(
        self,
        policy: EffectiveQuotaPolicy | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if policy is None:
            return None
        if isinstance(policy, EffectiveQuotaPolicy):
            return {
                "id": policy.id,
                "scope_type": policy.scope_type,
                "scope_id": policy.scope_id,
                "window_start": policy.window_start,
                "window_end": policy.window_end,
                "total_token_limit": policy.total_token_limit,
                "is_active": policy.is_active,
                "policy_version": policy.policy_version,
            }

        serialized = dict(policy)
        serialized["window_start"] = serialize_datetime(serialized.get("window_start"))
        serialized["window_end"] = serialize_datetime(serialized.get("window_end"))
        serialized["created_at"] = serialize_datetime(serialized.get("created_at"))
        serialized["updated_at"] = serialize_datetime(serialized.get("updated_at"))
        return serialized

    def _serialize_team_token_auth_config(
        self,
        row: dict[str, Any] | Any | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        serialized = dict(row)
        public_key = str(serialized.get("public_key") or "").strip()
        if not public_key:
            return None
        serialized["config_key"] = (
            str(serialized.get("config_key") or TEAM_TOKEN_AUTH_CONFIG_KEY).strip()
            or TEAM_TOKEN_AUTH_CONFIG_KEY
        )
        serialized["public_key"] = public_key
        serialized["config_version"] = int(serialized.get("config_version") or 0)
        serialized["created_at"] = serialize_datetime(serialized.get("created_at"))
        serialized["updated_at"] = serialize_datetime(serialized.get("updated_at"))
        return serialized

    def _policy_is_active_now(
        self,
        policy_row: dict[str, Any] | Any,
        now: datetime,
    ) -> bool:
        if not policy_row["is_active"]:
            return False
        if policy_row["window_start"] and policy_row["window_start"] > now:
            return False
        if policy_row["window_end"] and policy_row["window_end"] < now:
            return False
        return True

    async def _resolve_user_usage_summary(
        self,
        *,
        identifier: str,
        global_policy: dict[str, Any] | None,
        summary_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if global_policy is None:
            return {
                "user_identifier": identifier,
                "used_tokens": 0,
                "reserved_tokens": 0,
                "limit_tokens": 0,
                "policy_version": 0,
                "window_start": None,
                "window_end": None,
                "updated_at": None,
            }
        if self._summary_matches_policy(summary_row, global_policy):
            return dict(summary_row or {})

        used_tokens = await self._sum_user_window_usage(
            identifier=identifier,
            window_start=global_policy.get("window_start"),
            window_end=global_policy.get("window_end"),
        )
        refreshed = {
            "user_identifier": identifier,
            "used_tokens": used_tokens,
            "reserved_tokens": 0,
            "limit_tokens": int(global_policy["total_token_limit"]),
            "policy_version": int(global_policy.get("policy_version") or 0),
            "window_start": global_policy.get("window_start"),
            "window_end": global_policy.get("window_end"),
            "updated_at": global_policy.get("updated_at") or serialize_datetime(utcnow()),
        }
        await self._upsert_user_window_summary(refreshed)
        return refreshed

    def _summary_matches_policy(
        self,
        summary_row: dict[str, Any] | None,
        global_policy: dict[str, Any],
    ) -> bool:
        if not summary_row:
            return False
        return (
            int(summary_row.get("limit_tokens") or 0)
            == int(global_policy.get("total_token_limit") or 0)
            and int(summary_row.get("policy_version") or 0)
            == int(global_policy.get("policy_version") or 0)
            and serialize_datetime(summary_row.get("window_start"))
            == serialize_datetime(global_policy.get("window_start"))
            and serialize_datetime(summary_row.get("window_end"))
            == serialize_datetime(global_policy.get("window_end"))
        )

    async def _sum_user_window_usage(
        self,
        *,
        identifier: str,
        window_start: str | None,
        window_end: str | None,
    ) -> int:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    AGGREGATE_WINDOW_USAGE_SQL,
                    {
                        "user_identifier": identifier,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).mappings().first()
        return int((row or {}).get("used_tokens") or 0)

    async def _upsert_user_window_summary(self, row: dict[str, Any]) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                UPSERT_USER_WINDOW_SUMMARY_SQL,
                {
                    "user_identifier": row["user_identifier"],
                    "used_tokens": int(row.get("used_tokens") or 0),
                    "reserved_tokens": int(row.get("reserved_tokens") or 0),
                    "limit_tokens": int(row.get("limit_tokens") or 0),
                    "policy_version": int(row.get("policy_version") or 0),
                    "window_start": self._deserialize_datetime(row.get("window_start")),
                    "window_end": self._deserialize_datetime(row.get("window_end")),
                    "updated_at": self._deserialize_datetime(
                        row.get("updated_at")
                    )
                    or utcnow(),
                },
            )

    def _deserialize_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))
