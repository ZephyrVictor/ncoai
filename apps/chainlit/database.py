from __future__ import annotations

import hashlib
import json
import ssl
from typing import TYPE_CHECKING, Any

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import queue_until_user_message
from chainlit.logger import logger
from chainlit.types import PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from ncoai_shared.bootstrap import ensure_application_schema
from settings import DataCacheSettings, DatabasePoolSettings

if TYPE_CHECKING:
    from chainlit.step import StepDict


def build_async_engine_kwargs(pool_settings: DatabasePoolSettings) -> dict[str, Any]:
    return {
        "pool_size": pool_settings.size,
        "max_overflow": pool_settings.max_overflow,
        "pool_timeout": pool_settings.timeout_seconds,
        "pool_recycle": pool_settings.recycle_seconds,
        "pool_pre_ping": pool_settings.pre_ping,
        "pool_use_lifo": pool_settings.use_lifo,
    }


def create_app_engine(
    database_url: str,
    *,
    pool_settings: DatabasePoolSettings,
) -> AsyncEngine:
    return create_async_engine(
        database_url,
        **build_async_engine_kwargs(pool_settings),
    )


class ConfiguredSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    CACHE_PREFIX = "chainlit:data-cache"

    def __init__(
        self,
        conninfo: str,
        connect_args: dict[str, Any] | None = None,
        ssl_require: bool = False,
        storage_provider: BaseStorageClient | None = None,
        user_thread_limit: int | None = 1000,
        show_logger: bool | None = False,
        *,
        engine: AsyncEngine | None = None,
        pool_settings: DatabasePoolSettings | None = None,
        cache_settings: DataCacheSettings | None = None,
        cache_url: str | None = None,
    ) -> None:
        self._conninfo = conninfo
        self.user_thread_limit = user_thread_limit
        self.show_logger = show_logger
        self._owns_engine = engine is None
        self._cache_settings = cache_settings
        self._cache_url = cache_url
        self._cache: Redis | None = None
        if connect_args is None:
            connect_args = {}
        if ssl_require:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ssl_context
        if engine is not None:
            self.engine = engine
        else:
            if pool_settings is None:
                raise ValueError("pool_settings is required when engine is not provided")
            self.engine = create_async_engine(
                self._conninfo,
                connect_args=connect_args,
                **build_async_engine_kwargs(pool_settings),
            )
        self.async_session = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        if storage_provider:
            self.storage_provider = storage_provider
            if self.show_logger:
                logger.info("SQLAlchemyDataLayer storage client initialized")
        else:
            self.storage_provider = None
            logger.warning(
                "SQLAlchemyDataLayer storage client is not initialized and elements will not be persisted!"
            )

    async def _get_cache(self) -> Redis | None:
        if (
            self._cache_settings is None
            or not self._cache_settings.enabled
            or not self._cache_url
        ):
            return None
        if self._cache is None:
            self._cache = Redis.from_url(self._cache_url, decode_responses=True)
        return self._cache

    def _cache_ttl_seconds(self) -> int:
        if self._cache_settings is None:
            return 0
        return max(self._cache_settings.ttl_seconds, 1)

    def _persisted_user_cache_key(self, identifier: str) -> str:
        return f"{self.CACHE_PREFIX}:user:{identifier}"

    def _thread_owner_cache_key(self, thread_id: str) -> str:
        return f"{self.CACHE_PREFIX}:thread-owner:{thread_id}"

    def _thread_version_cache_key(self, thread_id: str) -> str:
        return f"{self.CACHE_PREFIX}:thread-version:{thread_id}"

    def _thread_payload_cache_key(self, thread_id: str, version: int) -> str:
        return f"{self.CACHE_PREFIX}:thread:{thread_id}:v{version}"

    def _thread_author_cache_key(self, thread_id: str, version: int) -> str:
        return f"{self.CACHE_PREFIX}:thread-author:{thread_id}:v{version}"

    def _thread_list_version_cache_key(self, user_id: str) -> str:
        return f"{self.CACHE_PREFIX}:thread-list-version:{user_id}"

    def _thread_list_cache_key(
        self,
        *,
        user_id: str,
        version: int,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> str:
        payload = {
            "user_id": user_id,
            "version": version,
            "pagination": pagination.model_dump(mode="json"),
            "filters": filters.model_dump(mode="json"),
        }
        digest = hashlib.sha1(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"{self.CACHE_PREFIX}:thread-list:{user_id}:{digest}"

    async def _cache_get_json(self, key: str) -> Any | None:
        cache = await self._get_cache()
        if cache is None:
            return None
        payload = await cache.get(key)
        if not payload:
            return None
        return json.loads(payload)

    async def _cache_set_json(self, key: str, value: Any) -> None:
        cache = await self._get_cache()
        if cache is None:
            return
        await cache.set(
            key,
            json.dumps(value, ensure_ascii=False),
            ex=self._cache_ttl_seconds(),
        )

    async def _cache_get_int(self, key: str) -> int:
        cache = await self._get_cache()
        if cache is None:
            return 0
        value = await cache.get(key)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def _store_thread_owner(
        self,
        *,
        thread_id: str,
        user_id: str | None,
        user_identifier: str | None,
    ) -> None:
        if not thread_id:
            return
        await self._cache_set_json(
            self._thread_owner_cache_key(thread_id),
            {
                "user_id": user_id,
                "user_identifier": user_identifier,
            },
        )

    async def _resolve_thread_owner(self, thread_id: str) -> dict[str, str | None]:
        cached = await self._cache_get_json(self._thread_owner_cache_key(thread_id))
        if isinstance(cached, dict):
            return {
                "user_id": cached.get("user_id"),
                "user_identifier": cached.get("user_identifier"),
            }

        result = await self.execute_sql(
            query='SELECT "userId", "userIdentifier" FROM threads WHERE "id" = :thread_id',
            parameters={"thread_id": thread_id},
        )
        owner = {"user_id": None, "user_identifier": None}
        if isinstance(result, list) and result:
            owner = {
                "user_id": result[0].get("userId"),
                "user_identifier": result[0].get("userIdentifier"),
            }
            await self._store_thread_owner(
                thread_id=thread_id,
                user_id=owner["user_id"],
                user_identifier=owner["user_identifier"],
            )
        return owner

    async def _invalidate_thread_views(
        self,
        *,
        thread_id: str | None,
        user_id: str | None = None,
        user_identifier: str | None = None,
        invalidate_thread_detail: bool = True,
        invalidate_thread_list: bool = True,
        drop_owner_cache: bool = False,
    ) -> None:
        if not thread_id:
            return
        cache = await self._get_cache()
        if cache is None:
            return

        resolved_user_id = user_id
        resolved_user_identifier = user_identifier
        if invalidate_thread_list and (
            resolved_user_id is None or resolved_user_identifier is None
        ):
            owner = await self._resolve_thread_owner(thread_id)
            resolved_user_id = resolved_user_id or owner.get("user_id")
            resolved_user_identifier = resolved_user_identifier or owner.get(
                "user_identifier"
            )

        pipe = cache.pipeline(transaction=False)
        if invalidate_thread_detail:
            pipe.incr(self._thread_version_cache_key(thread_id))
        if invalidate_thread_list and resolved_user_id:
            pipe.incr(self._thread_list_version_cache_key(resolved_user_id))
        if drop_owner_cache:
            pipe.delete(self._thread_owner_cache_key(thread_id))
        await pipe.execute()

    def _step_requires_thread_list_invalidation(self, step_dict: "StepDict") -> bool:
        if bool(step_dict.get("streaming")):
            return False
        metadata = step_dict.get("metadata") or {}
        if isinstance(metadata, dict):
            status = str(metadata.get("generation_status") or "").strip().lower()
            if status in {"pending", "queued"}:
                return False
        return True

    async def _lookup_thread_id_for_step(self, step_id: str) -> str | None:
        result = await self.execute_sql(
            query='SELECT "threadId" FROM steps WHERE "id" = :step_id',
            parameters={"step_id": step_id},
        )
        if isinstance(result, list) and result:
            return result[0].get("threadId")
        return None

    async def _lookup_thread_id_for_feedback(self, feedback_id: str) -> str | None:
        result = await self.execute_sql(
            query=(
                'SELECT s."threadId" FROM feedbacks f '
                'JOIN steps s ON f."forId" = s."id" '
                'WHERE f."id" = :feedback_id'
            ),
            parameters={"feedback_id": feedback_id},
        )
        if isinstance(result, list) and result:
            return result[0].get("threadId")
        return None

    async def _lookup_thread_id_for_element(self, element_id: str) -> str | None:
        result = await self.execute_sql(
            query='SELECT "threadId" FROM elements WHERE "id" = :element_id',
            parameters={"element_id": element_id},
        )
        if isinstance(result, list) and result:
            return result[0].get("threadId")
        return None

    async def get_user(self, identifier: str) -> PersistedUser | None:
        cached = await self._cache_get_json(self._persisted_user_cache_key(identifier))
        if isinstance(cached, dict):
            return PersistedUser(
                id=str(cached["id"]),
                identifier=str(cached["identifier"]),
                createdAt=str(cached["createdAt"]),
                metadata=dict(cached.get("metadata") or {}),
            )

        persisted_user = await super().get_user(identifier)
        if persisted_user is not None:
            await self._cache_set_json(
                self._persisted_user_cache_key(identifier),
                {
                    "id": persisted_user.id,
                    "identifier": persisted_user.identifier,
                    "createdAt": persisted_user.createdAt,
                    "metadata": persisted_user.metadata or {},
                },
            )
        return persisted_user

    async def create_user(self, user: User) -> PersistedUser | None:
        persisted_user = await super().create_user(user)
        cache = await self._get_cache()
        if cache is not None:
            await cache.delete(self._persisted_user_cache_key(user.identifier))
        if persisted_user is not None:
            await self._cache_set_json(
                self._persisted_user_cache_key(user.identifier),
                {
                    "id": persisted_user.id,
                    "identifier": persisted_user.identifier,
                    "createdAt": persisted_user.createdAt,
                    "metadata": persisted_user.metadata or {},
                },
            )
        return persisted_user

    async def get_thread_author(self, thread_id: str) -> str:
        version = await self._cache_get_int(self._thread_version_cache_key(thread_id))
        cached = await self._cache_get_json(
            self._thread_author_cache_key(thread_id, version)
        )
        if isinstance(cached, dict) and cached.get("author"):
            return str(cached["author"])

        owner = await self._resolve_thread_owner(thread_id)
        author = owner.get("user_identifier")
        if author:
            await self._cache_set_json(
                self._thread_author_cache_key(thread_id, version),
                {"author": author},
            )
            return author

        author = await super().get_thread_author(thread_id)
        await self._store_thread_owner(
            thread_id=thread_id,
            user_id=owner.get("user_id"),
            user_identifier=author,
        )
        await self._cache_set_json(
            self._thread_author_cache_key(thread_id, version),
            {"author": author},
        )
        return author

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        version = await self._cache_get_int(self._thread_version_cache_key(thread_id))
        cached = await self._cache_get_json(
            self._thread_payload_cache_key(thread_id, version)
        )
        if isinstance(cached, dict):
            return cached

        thread = await super().get_thread(thread_id)
        if thread is not None:
            await self._store_thread_owner(
                thread_id=thread_id,
                user_id=thread.get("userId"),
                user_identifier=thread.get("userIdentifier"),
            )
            await self._cache_set_json(
                self._thread_payload_cache_key(thread_id, version),
                thread,
            )
        return thread

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse:
        if not filters.userId:
            return await super().list_threads(pagination, filters)

        version = await self._cache_get_int(
            self._thread_list_version_cache_key(filters.userId)
        )
        cache_key = self._thread_list_cache_key(
            user_id=filters.userId,
            version=version,
            pagination=pagination,
            filters=filters,
        )
        cached = await self._cache_get_json(cache_key)
        if isinstance(cached, dict):
            return PaginatedResponse(
                pageInfo=PageInfo.from_dict(cached.get("pageInfo", {})),
                data=list(cached.get("data", [])),
            )

        result = await super().list_threads(pagination, filters)
        for thread in result.data:
            if isinstance(thread, dict):
                await self._store_thread_owner(
                    thread_id=str(thread.get("id") or ""),
                    user_id=thread.get("userId"),
                    user_identifier=thread.get("userIdentifier"),
                )
        await self._cache_set_json(cache_key, result.to_dict())
        return result

    def _step_parameters(self, step_dict: "StepDict") -> dict[str, Any]:
        normalized = dict(step_dict)
        normalized["showInput"] = (
            str(normalized.get("showInput", "")).lower()
            if "showInput" in normalized
            else None
        )
        parameters = {
            key: value
            for key, value in normalized.items()
            if value is not None and not (isinstance(value, dict) and not value)
        }
        parameters["metadata"] = json.dumps(normalized.get("metadata", {}))
        parameters["generation"] = json.dumps(normalized.get("generation", {}))
        return parameters

    async def _upsert_step(self, step_dict: "StepDict") -> None:
        parameters = self._step_parameters(step_dict)
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        updates = ", ".join(
            f'"{key}" = :{key}' for key in parameters.keys() if key != "id"
        )
        query = f"""
            INSERT INTO steps ({columns})
            VALUES ({values})
            ON CONFLICT (id) DO UPDATE
            SET {updates};
        """
        await self.execute_sql(query=query, parameters=parameters)

    @queue_until_user_message()
    async def create_step(self, step_dict: "StepDict"):
        await super().update_thread(step_dict["threadId"])
        if self.show_logger:
            logger.info(f"SQLAlchemy: create_step, step_id={step_dict.get('id')}")
        await self._upsert_step(step_dict)
        await self._invalidate_thread_views(thread_id=step_dict.get("threadId"))

    @queue_until_user_message()
    async def update_step(self, step_dict: "StepDict"):
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_step, step_id={step_dict.get('id')}")
        await self._upsert_step(step_dict)
        await self._invalidate_thread_views(
            thread_id=step_dict.get("threadId"),
            invalidate_thread_detail=True,
            invalidate_thread_list=self._step_requires_thread_list_invalidation(
                step_dict
            ),
        )

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ):
        await super().update_thread(
            thread_id=thread_id,
            name=name,
            user_id=user_id,
            metadata=metadata,
            tags=tags,
        )
        user_identifier = None
        if user_id:
            try:
                user_identifier = await self._get_user_identifer_by_id(user_id)
            except Exception:
                user_identifier = None
            await self._store_thread_owner(
                thread_id=thread_id,
                user_id=user_id,
                user_identifier=user_identifier,
            )
        await self._invalidate_thread_views(
            thread_id=thread_id,
            user_id=user_id,
            user_identifier=user_identifier,
        )

    async def delete_thread(self, thread_id: str):
        owner = await self._resolve_thread_owner(thread_id)
        await super().delete_thread(thread_id)
        await self._invalidate_thread_views(
            thread_id=thread_id,
            user_id=owner.get("user_id"),
            user_identifier=owner.get("user_identifier"),
            drop_owner_cache=True,
        )

    async def upsert_feedback(self, feedback: Feedback) -> str:
        feedback_id = await super().upsert_feedback(feedback)
        thread_id = await self._lookup_thread_id_for_step(feedback.forId)
        await self._invalidate_thread_views(thread_id=thread_id)
        return feedback_id

    async def delete_feedback(self, feedback_id: str) -> bool:
        thread_id = await self._lookup_thread_id_for_feedback(feedback_id)
        deleted = await super().delete_feedback(feedback_id)
        await self._invalidate_thread_views(thread_id=thread_id)
        return deleted

    @queue_until_user_message()
    async def create_element(self, element: "Element"):
        await super().create_element(element)
        await self._invalidate_thread_views(thread_id=element.thread_id)

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: str | None = None):
        resolved_thread_id = thread_id or await self._lookup_thread_id_for_element(
            element_id
        )
        await super().delete_element(element_id, thread_id=thread_id)
        await self._invalidate_thread_views(thread_id=resolved_thread_id)

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        thread_id = await self._lookup_thread_id_for_step(step_id)
        await super().delete_step(step_id)
        await self._invalidate_thread_views(thread_id=thread_id)

    async def close(self) -> None:
        if self.storage_provider:
            await self.storage_provider.close()
        if self._cache is not None:
            await self._cache.close()
            self._cache = None
        if self._owns_engine:
            await self.engine.dispose()


async def ensure_chainlit_schema(database_url: str) -> None:
    await ensure_application_schema(database_url)
