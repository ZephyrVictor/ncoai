from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import database as chainlit_database  # noqa: E402
import quota as chainlit_quota  # noqa: E402
import settings as chainlit_settings  # noqa: E402
from chainlit.types import PageInfo, PaginatedResponse, Pagination, ThreadFilter  # noqa: E402
from chainlit.user import PersistedUser  # noqa: E402


class FakeRedisPipeline:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.operations: list[tuple[str, str]] = []

    def incr(self, key: str):
        self.operations.append(("incr", key))
        return self

    def delete(self, key: str):
        self.operations.append(("delete", key))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for operation, key in self.operations:
            if operation == "incr":
                next_value = int(self.store.get(key, "0")) + 1
                self.store[key] = str(next_value)
                results.append(next_value)
            elif operation == "delete":
                self.store.pop(key, None)
                results.append(1)
        self.operations.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        del ex
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                self.store.pop(key, None)
        return deleted

    def pipeline(self, transaction: bool = False) -> FakeRedisPipeline:
        del transaction
        return FakeRedisPipeline(self.store)

    async def close(self) -> None:
        return None


def test_load_settings_reads_database_pool_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAINLIT_DB_POOL_SIZE", "32")
    monkeypatch.setenv("CHAINLIT_DB_MAX_OVERFLOW", "24")
    monkeypatch.setenv("CHAINLIT_DB_POOL_TIMEOUT_SECONDS", "9.5")
    monkeypatch.setenv("CHAINLIT_DB_POOL_RECYCLE_SECONDS", "900")
    monkeypatch.setenv("CHAINLIT_DB_POOL_PRE_PING", "0")
    monkeypatch.setenv("CHAINLIT_DB_POOL_USE_LIFO", "0")

    loaded = chainlit_settings.load_settings()

    assert loaded.database.pool.size == 32
    assert loaded.database.pool.max_overflow == 24
    assert loaded.database.pool.timeout_seconds == 9.5
    assert loaded.database.pool.recycle_seconds == 900
    assert loaded.database.pool.pre_ping is False
    assert loaded.database.pool.use_lifo is False


def test_load_settings_reads_litellm_runtime_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAINLIT_OPENAI_TIMEOUT", "95")
    monkeypatch.setenv("CHAINLIT_OPENAI_MAX_RETRIES", "1")
    monkeypatch.setenv("CHAINLIT_OPENAI_MAX_CONNECTIONS", "360")
    monkeypatch.setenv("CHAINLIT_OPENAI_MAX_KEEPALIVE_CONNECTIONS", "180")
    monkeypatch.setenv("CHAINLIT_LITELLM_MAX_CONCURRENT_REQUESTS", "220")
    monkeypatch.setenv("CHAINLIT_LITELLM_QUEUE_TIMEOUT_SECONDS", "6.5")

    loaded = chainlit_settings.load_settings()

    assert loaded.litellm.timeout_seconds == 95.0
    assert loaded.litellm.max_retries == 1
    assert loaded.litellm.max_connections == 360
    assert loaded.litellm.max_keepalive_connections == 180
    assert loaded.litellm.max_concurrent_requests == 220
    assert loaded.litellm.queue_timeout_seconds == 6.5


def test_load_settings_splits_shared_capacities_per_worker(monkeypatch) -> None:
    monkeypatch.setenv("CHAINLIT_WORKER_COUNT", "4")
    monkeypatch.setenv("CHAINLIT_WORKER_INDEX", "2")
    monkeypatch.setenv("CHAINLIT_DB_POOL_SIZE", "160")
    monkeypatch.setenv("CHAINLIT_DB_MAX_OVERFLOW", "96")
    monkeypatch.setenv("CHAINLIT_OPENAI_MAX_CONNECTIONS", "512")
    monkeypatch.setenv("CHAINLIT_OPENAI_MAX_KEEPALIVE_CONNECTIONS", "256")
    monkeypatch.setenv("CHAINLIT_LITELLM_MAX_CONCURRENT_REQUESTS", "220")
    monkeypatch.setenv("CHAINLIT_STREAM_SNAPSHOT_INTERVAL_SECONDS", "2")

    loaded = chainlit_settings.load_settings()

    assert loaded.runtime.worker_count == 4
    assert loaded.runtime.worker_index == 2
    assert loaded.runtime.worker_id == "chainlit-2"
    assert loaded.runtime.stream_snapshot_interval_seconds == 2.0
    assert loaded.database.pool.size == 40
    assert loaded.database.pool.max_overflow == 24
    assert loaded.litellm.max_connections == 128
    assert loaded.litellm.max_keepalive_connections == 64
    assert loaded.litellm.max_concurrent_requests == 55


def test_load_settings_reads_admission_and_metadata_flush_env(monkeypatch) -> None:
    monkeypatch.setenv("MAX_NUM_SEQS", "200")
    monkeypatch.setenv("CHAINLIT_GLOBAL_ADMISSION_MAX_INFLIGHT", "144")
    monkeypatch.setenv("CHAINLIT_GLOBAL_ADMISSION_QUEUE_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("CHAINLIT_METADATA_FLUSH_INTERVAL_SECONDS", "3")
    monkeypatch.setenv("CHAINLIT_STREAM_SNAPSHOT_INTERVAL_SECONDS", "10")

    loaded = chainlit_settings.load_settings()

    assert loaded.runtime.stream_snapshot_interval_seconds == 10.0
    assert loaded.admission.max_inflight == 144
    assert loaded.admission.queue_timeout_seconds == 4.5
    assert loaded.metadata_flush.interval_seconds == 3.0


def test_build_async_engine_kwargs_reflect_pool_settings() -> None:
    pool_settings = chainlit_settings.DatabasePoolSettings(
        size=20,
        max_overflow=30,
        timeout_seconds=12.5,
        recycle_seconds=1800,
        pre_ping=True,
        use_lifo=True,
    )

    assert chainlit_database.build_async_engine_kwargs(pool_settings) == {
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 12.5,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
    }


def test_create_app_engine_passes_pool_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_engine = object()
    pool_settings = chainlit_settings.DatabasePoolSettings(
        size=18,
        max_overflow=12,
        timeout_seconds=7.0,
        recycle_seconds=600,
        pre_ping=True,
        use_lifo=False,
    )

    def fake_create_async_engine(database_url: str, **kwargs: object) -> object:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return fake_engine

    monkeypatch.setattr(chainlit_database, "create_async_engine", fake_create_async_engine)

    engine = chainlit_database.create_app_engine(
        "postgresql+asyncpg://user:pass@localhost/db",
        pool_settings=pool_settings,
    )

    assert engine is fake_engine
    assert captured["database_url"] == "postgresql+asyncpg://user:pass@localhost/db"
    assert captured["kwargs"] == {
        "pool_size": 18,
        "max_overflow": 12,
        "pool_timeout": 7.0,
        "pool_recycle": 600,
        "pool_pre_ping": True,
        "pool_use_lifo": False,
    }


def test_configured_data_layer_reuses_provided_engine() -> None:
    fake_engine = object()

    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=fake_engine,
        storage_provider=None,
    )

    assert data_layer.engine is fake_engine
    assert data_layer.async_session.kw["bind"] is fake_engine


@pytest.mark.asyncio
async def test_quota_service_does_not_dispose_shared_engine() -> None:
    fake_engine = SimpleNamespace(dispose=AsyncMock())

    service = chainlit_quota.QuotaService(
        "postgresql+asyncpg://user:pass@localhost/db",
        settings=chainlit_quota.SETTINGS,
        engine=fake_engine,
    )

    await service.close()

    fake_engine.dispose.assert_not_called()


@pytest.mark.asyncio
async def test_quota_settle_avoids_reaggregating_window_usage(monkeypatch) -> None:
    service = chainlit_quota.QuotaService(
        "postgresql+asyncpg://user:pass@localhost/db",
        settings=chainlit_quota.SETTINGS,
        engine=object(),
    )
    reservation = chainlit_quota.QuotaReservation(
        request_id="request-1",
        step_id="step-1",
        thread_id="thread-1",
        user_identifier="user-1",
        prompt_tokens=32,
        reserved_tokens=96,
        allowed_completion_tokens=64,
        thread_summary=chainlit_quota.QuotaSummary(used_tokens=10, reserved_tokens=0, limit_tokens=1024),
        user_summary=chainlit_quota.QuotaSummary(used_tokens=20, reserved_tokens=0, limit_tokens=100000),
    )
    thread_summary = chainlit_quota.QuotaSummary(used_tokens=42, reserved_tokens=0, limit_tokens=1024)
    user_summary = chainlit_quota.QuotaSummary(used_tokens=84, reserved_tokens=0, limit_tokens=100000)
    sum_user_window_usage = AsyncMock(return_value=999)

    monkeypatch.setattr(service, "_ensure_ready", AsyncMock())
    service._redis = SimpleNamespace(eval=AsyncMock(return_value=[1]))
    monkeypatch.setattr(
        service,
        "_persist_settlement",
        AsyncMock(return_value=(thread_summary, user_summary)),
    )
    monkeypatch.setattr(
        service._config_provider,
        "get_thread_limit",
        AsyncMock(return_value=chainlit_quota.QuotaConfig(limit_tokens=1024)),
    )
    monkeypatch.setattr(
        service._config_provider,
        "get_user_limit",
        AsyncMock(return_value=chainlit_quota.QuotaConfig(limit_tokens=100000)),
    )
    monkeypatch.setattr(
        service,
        "_get_hot_summary",
        AsyncMock(side_effect=[thread_summary, user_summary]),
    )
    monkeypatch.setattr(service, "_set_hot_summary", AsyncMock())
    monkeypatch.setattr(service, "_persist_thread_metadata", AsyncMock())
    monkeypatch.setattr(service, "_apply_user_summary_status", lambda summary, config: summary)
    monkeypatch.setattr(
        service._repository,
        "sum_user_window_usage",
        sum_user_window_usage,
    )

    settlement = await service.settle(
        reservation=reservation,
        provider_request_id="provider-1",
        assistant_content="hello world",
        usage=SimpleNamespace(prompt_tokens=32, completion_tokens=52, total_tokens=84),
    )

    assert settlement.total_tokens == 84
    sum_user_window_usage.assert_not_called()


@pytest.mark.asyncio
async def test_quota_thread_metadata_callback_replaces_sync_write(monkeypatch) -> None:
    captured: list[tuple[str, dict[str, object]]] = []
    service = chainlit_quota.QuotaService(
        "postgresql+asyncpg://user:pass@localhost/db",
        settings=chainlit_quota.SETTINGS,
        engine=object(),
        thread_metadata_callback=lambda thread_id, patch: captured.append(
            (thread_id, dict(patch))
        ),
    )

    update_thread_metadata = AsyncMock()
    monkeypatch.setattr(
        service._repository,
        "update_thread_metadata",
        update_thread_metadata,
    )

    await service._persist_thread_metadata(
        "thread-1",
        chainlit_quota.QuotaSummary(
            used_tokens=12,
            reserved_tokens=0,
            limit_tokens=1024,
        ),
    )

    update_thread_metadata.assert_not_called()
    assert captured == [
        (
            "thread-1",
            {
                "token_usage_summary": {
                    "used_tokens": 12,
                    "reserved_tokens": 0,
                    "limit_tokens": 1024,
                    "remaining_tokens": 1012,
                    "policy_version": 0,
                    "updated_at": None,
                    "window_start": None,
                    "window_end": None,
                    "status": "ok",
                    "message": None,
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_step_avoids_redundant_thread_upsert(monkeypatch) -> None:
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
    )
    update_thread = AsyncMock()
    execute_sql = AsyncMock()
    monkeypatch.setattr(data_layer, "update_thread", update_thread)
    monkeypatch.setattr(data_layer, "execute_sql", execute_sql)

    await chainlit_database.ConfiguredSQLAlchemyDataLayer.update_step.__wrapped__(
        data_layer,
        {
            "id": "step-1",
            "threadId": "thread-1",
            "name": "Assistant",
            "type": "assistant_message",
            "output": "hello",
            "metadata": {"generation_status": "pending"},
            "generation": {},
        },
    )

    update_thread.assert_not_called()
    execute_sql.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_step_still_ensures_thread_exists(monkeypatch) -> None:
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
    )
    update_thread = AsyncMock()
    execute_sql = AsyncMock()
    monkeypatch.setattr(
        chainlit_database.SQLAlchemyDataLayer,
        "update_thread",
        update_thread,
    )
    monkeypatch.setattr(data_layer, "execute_sql", execute_sql)

    await chainlit_database.ConfiguredSQLAlchemyDataLayer.create_step.__wrapped__(
        data_layer,
        {
            "id": "step-1",
            "threadId": "thread-1",
            "name": "Assistant",
            "type": "assistant_message",
            "output": "hello",
            "metadata": {"generation_status": "pending"},
            "generation": {},
        },
    )

    update_thread.assert_awaited_once_with("thread-1")
    execute_sql.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_update_only_invalidates_thread_detail(monkeypatch) -> None:
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
        cache_settings=chainlit_settings.DataCacheSettings(enabled=True, ttl_seconds=15),
        cache_url="redis://example/0",
    )
    fake_redis = FakeRedis()
    monkeypatch.setattr(data_layer, "_cache", fake_redis)
    monkeypatch.setattr(data_layer, "_get_cache", AsyncMock(return_value=fake_redis))
    monkeypatch.setattr(data_layer, "execute_sql", AsyncMock())

    await chainlit_database.ConfiguredSQLAlchemyDataLayer.update_step.__wrapped__(
        data_layer,
        {
            "id": "step-1",
            "threadId": "thread-1",
            "name": "Assistant",
            "type": "assistant_message",
            "output": "hello",
            "metadata": {},
            "generation": {},
            "streaming": True,
        },
    )

    assert fake_redis.store[data_layer._thread_version_cache_key("thread-1")] == "1"
    assert data_layer._thread_list_version_cache_key("user-1") not in fake_redis.store


@pytest.mark.asyncio
async def test_get_user_uses_redis_cache(monkeypatch) -> None:
    fake_redis = FakeRedis()
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
        cache_settings=chainlit_settings.DataCacheSettings(enabled=True, ttl_seconds=30),
        cache_url="redis://cache",
    )
    monkeypatch.setattr(data_layer, "_get_cache", AsyncMock(return_value=fake_redis))
    fetch_user = AsyncMock(
        return_value=PersistedUser(
            id="user-id",
            identifier="demo-user",
            createdAt="2026-04-11T00:00:00Z",
            metadata={"role": "managed-user"},
        )
    )
    monkeypatch.setattr(
        chainlit_database.SQLAlchemyDataLayer,
        "get_user",
        fetch_user,
    )

    first = await data_layer.get_user("demo-user")
    second = await data_layer.get_user("demo-user")

    assert first is not None
    assert second is not None
    assert first.identifier == "demo-user"
    assert second.identifier == "demo-user"
    fetch_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_threads_cache_invalidates_after_thread_update(monkeypatch) -> None:
    fake_redis = FakeRedis()
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
        cache_settings=chainlit_settings.DataCacheSettings(enabled=True, ttl_seconds=30),
        cache_url="redis://cache",
    )
    monkeypatch.setattr(data_layer, "_get_cache", AsyncMock(return_value=fake_redis))
    list_threads = AsyncMock(
        return_value=PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=False,
                startCursor="thread-1",
                endCursor="thread-1",
            ),
            data=[
                {
                    "id": "thread-1",
                    "createdAt": "2026-04-11T00:00:00Z",
                    "name": "Thread 1",
                    "userId": "user-db-1",
                    "userIdentifier": "demo-user",
                    "tags": [],
                    "metadata": {"name": "Thread 1"},
                    "steps": [],
                    "elements": [],
                }
            ],
        )
    )
    monkeypatch.setattr(
        chainlit_database.SQLAlchemyDataLayer,
        "list_threads",
        list_threads,
    )
    update_thread = AsyncMock(return_value=None)
    monkeypatch.setattr(
        chainlit_database.SQLAlchemyDataLayer,
        "update_thread",
        update_thread,
    )
    monkeypatch.setattr(
        data_layer,
        "execute_sql",
        AsyncMock(
            return_value=[
                {
                    "id": "thread-1",
                    "userId": "user-db-1",
                    "userIdentifier": "demo-user",
                    "createdAt": "2026-04-11T00:00:00Z",
                }
            ]
        ),
    )
    get_user_identifier = AsyncMock(return_value="demo-user")
    monkeypatch.setattr(data_layer, "_get_user_identifer_by_id", get_user_identifier)

    pagination = Pagination(first=20, cursor=None)
    filters = ThreadFilter(userId="user-db-1", feedback=None, search=None)

    first = await data_layer.list_threads(pagination, filters)
    second = await data_layer.list_threads(pagination, filters)
    assert first.data[0]["id"] == "thread-1"
    assert second.data[0]["id"] == "thread-1"
    list_threads.assert_awaited_once()

    await data_layer.update_thread(
        thread_id="thread-1",
        user_id="user-db-1",
        metadata={"name": "Renamed"},
    )

    third = await data_layer.list_threads(pagination, filters)
    assert third.data[0]["id"] == "thread-1"
    assert list_threads.await_count == 2


@pytest.mark.asyncio
async def test_metadata_only_update_thread_bootstraps_thread_identity(monkeypatch) -> None:
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
    )
    super_update = AsyncMock(return_value=None)
    invalidate = AsyncMock(return_value=None)
    store_owner = AsyncMock(return_value=None)
    get_user_identifier = AsyncMock(return_value="demo-user")
    execute_sql = AsyncMock(return_value=[])
    infer_session_user_id = AsyncMock(return_value="user-db-1")

    monkeypatch.setattr(
        chainlit_database.SQLAlchemyDataLayer,
        "update_thread",
        super_update,
    )
    monkeypatch.setattr(data_layer, "execute_sql", execute_sql)
    monkeypatch.setattr(data_layer, "_invalidate_thread_views", invalidate)
    monkeypatch.setattr(data_layer, "_store_thread_owner", store_owner)
    monkeypatch.setattr(data_layer, "_get_user_identifer_by_id", get_user_identifier)
    monkeypatch.setattr(data_layer, "_infer_session_user_id", infer_session_user_id)

    await data_layer.update_thread(
        thread_id="thread-1",
        metadata={"chat_profile": "qwen35b_think"},
    )

    assert super_update.await_args_list == [
        call(thread_id="thread-1", user_id="user-db-1"),
        call(
            thread_id="thread-1",
            name=None,
            user_id="user-db-1",
            metadata={"chat_profile": "qwen35b_think"},
            tags=None,
        ),
    ]
    store_owner.assert_awaited_once_with(
        thread_id="thread-1",
        user_id="user-db-1",
        user_identifier="demo-user",
    )
    invalidate.assert_awaited_once_with(
        thread_id="thread-1",
        user_id="user-db-1",
        user_identifier="demo-user",
    )


@pytest.mark.asyncio
async def test_get_thread_author_recovers_identifier_from_user_id(monkeypatch) -> None:
    data_layer = chainlit_database.ConfiguredSQLAlchemyDataLayer(
        "postgresql+asyncpg://user:pass@localhost/db",
        engine=object(),
        storage_provider=None,
        cache_settings=chainlit_settings.DataCacheSettings(enabled=True, ttl_seconds=30),
        cache_url="redis://cache",
    )
    fake_redis = FakeRedis()
    store_owner = AsyncMock(return_value=None)

    monkeypatch.setattr(data_layer, "_get_cache", AsyncMock(return_value=fake_redis))
    monkeypatch.setattr(
        data_layer,
        "_resolve_thread_owner",
        AsyncMock(return_value={"user_id": "user-db-1", "user_identifier": None}),
    )
    monkeypatch.setattr(
        data_layer,
        "_get_user_identifer_by_id",
        AsyncMock(return_value="demo-user"),
    )
    monkeypatch.setattr(data_layer, "_store_thread_owner", store_owner)

    author = await data_layer.get_thread_author("thread-1")

    assert author == "demo-user"
    store_owner.assert_awaited_once_with(
        thread_id="thread-1",
        user_id="user-db-1",
        user_identifier="demo-user",
    )
