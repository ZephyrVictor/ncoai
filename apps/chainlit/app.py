from __future__ import annotations

import asyncio
import html
import importlib
import inspect
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote, urlsplit

import chainlit as cl
import httpx
from admission import AdmissionLease, GlobalAdmissionController
from chainlit.auth import (
    create_jwt,
    decode_jwt,
    get_current_user,
    reuseable_oauth,
    set_auth_cookie,
)
from chainlit.config import config
from chainlit.context import context
from chainlit.data import get_data_layer
from chainlit.server import app as chainlit_app, sio
from chainlit.types import ChatProfile, ThreadDict
from chainlit.user import PersistedUser, User
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from redis.asyncio import Redis

from caps import (
    CapsFTPServerRuntime,
    ensure_caps_storage_root_ready,
)
from database import ConfiguredSQLAlchemyDataLayer, create_app_engine
from gzctf_team_token import (
    parse_team_token,
    verify_gzctf_team_token,
)
from generation_registry import ActiveGeneration, GenerationRegistry
from metadata_buffer import ThreadMetadataBuffer
from ncoai_shared.bootstrap import (
    bootstrap_team_token_auth_config as bootstrap_team_token_config,
)
from ncoai_shared.controlplane import (
    TEAM_TOKEN_AUTH_PASSWORD_HASH,
    ControlPlaneRepository,
)
from ncoai_shared.monitoring import AppMetrics, classify_exception_result
from ncoai_shared.security import verify_password
from ncoai_shared.team_token_auth import (
    TEAM_TOKEN_AUTH_CONFIG_KEY,
    TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED,
    TEAM_TOKEN_AUTH_EVENTS_CHANNEL,
    TEAM_TOKEN_AUTH_VERSION_METADATA_KEY,
    load_cached_team_token_auth_config,
    parse_team_token_auth_event,
    store_cached_team_token_auth_config,
)
from quota import QuotaExceededError, QuotaService
from settings import SETTINGS

LOGGER = logging.getLogger("ncoai.chainlit")
chainlit_socket = importlib.import_module("chainlit.socket")

if SETTINGS.team_token_auth.mode == "team_token":
    os.environ["CHAINLIT_CUSTOM_AUTH"] = "1"

CONTROL_PLANE_ENGINE = create_app_engine(
    SETTINGS.database.async_url,
    pool_settings=SETTINGS.database.pool,
)
CONTROL_PLANE_REPOSITORY = ControlPlaneRepository(CONTROL_PLANE_ENGINE)
DATA_LAYER = ConfiguredSQLAlchemyDataLayer(
    SETTINGS.database.async_url,
    engine=CONTROL_PLANE_ENGINE,
    cache_settings=SETTINGS.data_cache,
    cache_url=SETTINGS.redis.url,
    storage_provider=None,
)
QUOTA_SERVICE = QuotaService(
    SETTINGS.database.async_url,
    settings=SETTINGS,
    engine=CONTROL_PLANE_ENGINE,
)
APP_METRICS = AppMetrics("chainlit")
TEAM_TOKEN_AUTH_CACHE_REDIS: Redis | None = None
TEAM_TOKEN_AUTH_EVENTS_TASK: asyncio.Task[None] | None = None
TEAM_TOKEN_AUTH_LAST_VERSION = 0
CAPS_FTP_RUNTIME: CapsFTPServerRuntime | None = None
CHAINLIT_WORKER_ID = SETTINGS.runtime.worker_id
CHAINLIT_WORKER_COUNT = SETTINGS.runtime.worker_count
LITELLM_MAX_CONNECTIONS = max(SETTINGS.litellm.max_connections, 1)
LITELLM_MAX_KEEPALIVE_CONNECTIONS = min(
    max(SETTINGS.litellm.max_keepalive_connections, 1),
    LITELLM_MAX_CONNECTIONS,
)
LITELLM_HTTP_CLIENT = DefaultAsyncHttpxClient(
    timeout=httpx.Timeout(SETTINGS.litellm.timeout_seconds),
    limits=httpx.Limits(
        max_connections=LITELLM_MAX_CONNECTIONS,
        max_keepalive_connections=LITELLM_MAX_KEEPALIVE_CONNECTIONS,
    ),
)
LITELLM_CLIENT = AsyncOpenAI(
    base_url=SETTINGS.litellm.base_url,
    api_key=SETTINGS.litellm.api_key,
    timeout=SETTINGS.litellm.timeout_seconds,
    max_retries=SETTINGS.litellm.max_retries,
    http_client=LITELLM_HTTP_CLIENT,
)
LITELLM_REQUEST_SEMAPHORE = asyncio.Semaphore(
    max(SETTINGS.litellm.max_concurrent_requests, 1)
)
GLOBAL_ADMISSION_CONTROLLER = GlobalAdmissionController(
    redis_url=SETTINGS.redis.url,
    max_inflight=SETTINGS.admission.max_inflight,
    queue_timeout_seconds=SETTINGS.admission.queue_timeout_seconds,
    worker_id=CHAINLIT_WORKER_ID,
)
GENERATION_REGISTRY = GenerationRegistry()
THREAD_METADATA_BUFFER = ThreadMetadataBuffer(
    data_layer=DATA_LAYER,
    interval_seconds=SETTINGS.metadata_flush.interval_seconds,
)

LOGGER.info(
    "Configured Chainlit runtime worker_id=%s worker_index=%s worker_count=%s snapshot_interval=%ss",
    CHAINLIT_WORKER_ID,
    SETTINGS.runtime.worker_index,
    CHAINLIT_WORKER_COUNT,
    SETTINGS.runtime.stream_snapshot_interval_seconds,
)
LOGGER.info(
    "Configured Chainlit DB pool size=%s max_overflow=%s timeout=%ss recycle=%ss pre_ping=%s use_lifo=%s",
    SETTINGS.database.pool.size,
    SETTINGS.database.pool.max_overflow,
    SETTINGS.database.pool.timeout_seconds,
    SETTINGS.database.pool.recycle_seconds,
    SETTINGS.database.pool.pre_ping,
    SETTINGS.database.pool.use_lifo,
)
LOGGER.info(
    "Configured LiteLLM client timeout=%ss max_retries=%s max_connections=%s max_keepalive=%s max_inflight=%s queue_timeout=%ss",
    SETTINGS.litellm.timeout_seconds,
    SETTINGS.litellm.max_retries,
    LITELLM_MAX_CONNECTIONS,
    LITELLM_MAX_KEEPALIVE_CONNECTIONS,
    SETTINGS.litellm.max_concurrent_requests,
    SETTINGS.litellm.queue_timeout_seconds,
)
LOGGER.info(
    "Configured global admission max_inflight=%s queue_timeout=%ss",
    SETTINGS.admission.max_inflight,
    SETTINGS.admission.queue_timeout_seconds,
)
LOGGER.info(
    "Configured async metadata flush interval=%ss",
    SETTINGS.metadata_flush.interval_seconds,
)

try:
    asyncio.run(QUOTA_SERVICE.initialize())
except Exception as exc:  # pragma: no cover - operational path
    LOGGER.exception("Quota service initialization failed: %s", exc)


@chainlit_app.middleware("http")
async def instrument_requests(request: Request, call_next) -> Response:
    response = await APP_METRICS.instrument_http(request, call_next)
    response.headers["X-NCOAI-Chainlit-Worker"] = CHAINLIT_WORKER_ID
    return response


@cl.data_layer
def chainlit_data_layer() -> ConfiguredSQLAlchemyDataLayer:
    return DATA_LAYER


def thinking_enabled() -> bool:
    return SETTINGS.models.thinking_enabled


def team_token_auth_enabled() -> bool:
    return SETTINGS.team_token_auth.mode == "team_token"


def caps_enabled() -> bool:
    return SETTINGS.caps.enabled


def chainlit_root_path() -> str:
    root_path = (config.run.root_path or SETTINGS.chainlit_root_path or "").rstrip("/")
    return "" if root_path == "/" else root_path


def chainlit_path(path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    root_path = chainlit_root_path()
    return f"{root_path}{normalized_path}" if root_path else normalized_path


def chainlit_home_path() -> str:
    return chainlit_root_path() or "/"


def caps_storage_root() -> Path:
    return Path(SETTINGS.caps.storage_root).expanduser()


def caps_game_root() -> Path:
    return caps_storage_root() / f"game-{fixed_game_id()}"


def fixed_game_id() -> int:
    game_id = SETTINGS.team_token_auth.fixed_game_id
    if game_id <= 0:
        raise ValueError("GZCTF_FIXED_GAME_ID must be a positive integer")
    return game_id


def parse_config_version(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def sanitize_next_path(value: Any, *, fallback: str | None = None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return fallback

    parsed = urlsplit(normalized)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return fallback
    return normalized


def login_redirect_path(next_path: str | None) -> str:
    login_path = chainlit_path("/login")
    safe_next = sanitize_next_path(next_path)
    if not safe_next:
        return login_path
    return f"{login_path}?next={quote(safe_next, safe='')}"


async def get_team_token_auth_cache_redis() -> Redis:
    global TEAM_TOKEN_AUTH_CACHE_REDIS

    if TEAM_TOKEN_AUTH_CACHE_REDIS is not None:
        return TEAM_TOKEN_AUTH_CACHE_REDIS

    redis = Redis.from_url(SETTINGS.redis.url, decode_responses=True)
    await redis.ping()
    TEAM_TOKEN_AUTH_CACHE_REDIS = redis
    return redis


async def get_team_token_auth_config() -> dict[str, Any]:
    global TEAM_TOKEN_AUTH_LAST_VERSION

    redis: Redis | None = None
    try:
        redis = await get_team_token_auth_cache_redis()
        cached = await load_cached_team_token_auth_config(redis)
        if cached is not None:
            TEAM_TOKEN_AUTH_LAST_VERSION = max(
                TEAM_TOKEN_AUTH_LAST_VERSION,
                cached.config_version,
            )
            return cached.to_dict()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning("Failed to load cached team token auth config: %s", exc)

    config = await CONTROL_PLANE_REPOSITORY.get_team_token_auth_config()
    if config is None:
        config = await bootstrap_team_token_config(
            CONTROL_PLANE_ENGINE,
            public_key_base64=SETTINGS.team_token_auth.fixed_game_public_key,
            private_key_base64=SETTINGS.team_token_auth.fixed_game_private_key,
        )
    if config is None:
        raise ValueError("team token auth public key is not configured")

    TEAM_TOKEN_AUTH_LAST_VERSION = max(
        TEAM_TOKEN_AUTH_LAST_VERSION,
        parse_config_version(config.get("config_version")),
    )

    if redis is not None:
        try:
            await store_cached_team_token_auth_config(redis, config)
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning("Failed to store cached team token auth config: %s", exc)

    return config


async def validate_team_token_auth_token_version(token: str | None) -> int:
    if not team_token_auth_enabled():
        return 0
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        token_user = decode_jwt(token)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
        ) from exc

    config = await get_team_token_auth_config()
    if str(config.get("config_key") or TEAM_TOKEN_AUTH_CONFIG_KEY) != TEAM_TOKEN_AUTH_CONFIG_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    expected_version = parse_config_version(config.get("config_version"))
    token_version = parse_config_version(
        (token_user.metadata or {}).get(TEAM_TOKEN_AUTH_VERSION_METADATA_KEY)
    )
    if token_version != expected_version:
        raise HTTPException(
            status_code=401,
            detail="Authentication expired. Please log in again.",
        )
    return expected_version


async def get_current_user_with_team_token_version_guard(
    token: str | None = Depends(reuseable_oauth),
) -> PersistedUser | User | None:
    current_user = await get_current_user(token=token)
    if not current_user or not team_token_auth_enabled():
        return current_user
    await validate_team_token_auth_token_version(token)
    return current_user


async def get_optional_current_user_with_team_token_version_guard(
    token: str | None = Depends(reuseable_oauth),
) -> PersistedUser | User | None:
    if not token:
        return None
    try:
        return await get_current_user_with_team_token_version_guard(token=token)
    except HTTPException:
        return None


async def current_team_token_session_version_valid() -> bool:
    if not team_token_auth_enabled():
        return True

    session_token = getattr(context.session, "token", None)
    try:
        await validate_team_token_auth_token_version(session_token)
    except HTTPException:
        return False
    return True


def ensure_team_token_auth_config() -> None:
    if not team_token_auth_enabled():
        return

    fixed_game_id()


def current_user_identifier() -> str:
    session_user = getattr(context.session, "user", None)
    if session_user and getattr(session_user, "identifier", None):
        return session_user.identifier
    user = cl.user_session.get("user")
    if user and getattr(user, "identifier", None):
        return user.identifier
    return "anonymous"


async def current_managed_user() -> dict[str, Any] | None:
    identifier = current_user_identifier()
    if not identifier or identifier == "anonymous":
        return None
    return await CONTROL_PLANE_REPOSITORY.get_managed_user(
        identifier,
        include_deleted=True,
    )


def is_managed_user_active(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("is_active") and not user.get("is_deleted"))


async def is_subject_active(identifier: str) -> bool:
    managed_user = await CONTROL_PLANE_REPOSITORY.get_managed_user(
        identifier,
        include_deleted=True,
    )
    if team_token_auth_enabled():
        return managed_user is None or is_managed_user_active(managed_user)
    return is_managed_user_active(managed_user)


async def current_chat_subject_allowed() -> bool:
    managed_user = await current_managed_user()
    if team_token_auth_enabled():
        return managed_user is None or is_managed_user_active(managed_user)
    return is_managed_user_active(managed_user)


async def ensure_active_chat_session_allowed() -> bool:
    if not await current_team_token_session_version_valid():
        try:
            await context.emitter.emit("reload", {})
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning("Failed to emit reload for expired chat session: %s", exc)
        return False
    if not await current_chat_subject_allowed():
        await cl.Message(content="当前用户已被禁用，请联系管理员。").send()
        return False
    return True


def available_models() -> list[str]:
    models = [SETTINGS.models.default_model]
    if thinking_enabled():
        models.append(SETTINGS.models.thinking_model)
    return models


def resolve_model_alias() -> str:
    selected = cl.user_session.get("selected_model")
    if selected in available_models():
        return selected

    chat_profile = cl.user_session.get("chat_profile") or context.session.chat_profile
    if chat_profile in available_models():
        return chat_profile

    return SETTINGS.models.default_model


def build_client() -> AsyncOpenAI:
    return LITELLM_CLIENT


async def acquire_litellm_request_slot() -> tuple[bool, float]:
    wait_started = monotonic()
    try:
        await asyncio.wait_for(
            LITELLM_REQUEST_SEMAPHORE.acquire(),
            timeout=SETTINGS.litellm.queue_timeout_seconds,
        )
        return True, max(monotonic() - wait_started, 0.0)
    except asyncio.TimeoutError:
        return False, max(monotonic() - wait_started, 0.0)


def active_assistant_message_id() -> str | None:
    assistant_message_id = getattr(
        context.session, "active_generation_assistant_message_id", None
    )
    return str(assistant_message_id) if assistant_message_id else None


def message_metadata_with_status(
    status: str | None,
    *,
    token_usage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    if status:
        metadata["generation_status"] = status
    if token_usage:
        metadata["token_usage"] = token_usage
    return metadata or None


def clear_generation_status(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    next_metadata = dict(metadata)
    next_metadata.pop("generation_status", None)
    return next_metadata or None


class UpstreamQueueTimeoutError(RuntimeError):
    pass


async def close_async_resource(resource: Any | None, *, label: str) -> None:
    if resource is None:
        return

    close_method = getattr(resource, "close", None) or getattr(
        resource, "aclose", None
    )
    if not callable(close_method):
        return

    try:
        result = close_method()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning("Failed to close %s: %s", label, exc)


def quota_message_metadata(
    scope: str | None,
    message: str,
    thread_summary: dict[str, Any],
    user_summary: dict[str, Any],
) -> dict[str, Any]:
    updated_at = thread_summary.get("updated_at") or user_summary.get("updated_at")
    return {
        "quota_status": {"blocked_scope": scope, "reason": message},
        "token_usage": {
            "request_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "session_used_tokens": thread_summary.get("used_tokens", 0),
            "session_limit_tokens": thread_summary.get("limit_tokens", 0),
            "user_used_tokens": user_summary.get("used_tokens", 0),
            "user_limit_tokens": user_summary.get("limit_tokens", 0),
            "user_policy_version": user_summary.get("policy_version", 0),
            "updated_at": updated_at,
        },
    }


def parse_thread_metadata(thread: ThreadDict) -> dict[str, Any]:
    raw = thread.get("metadata") or {}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw if isinstance(raw, dict) else {}


def parse_step_metadata(step: Mapping[str, Any]) -> dict[str, Any]:
    raw = step.get("metadata") or {}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def find_step_by_id(
    steps: list[dict[str, Any]] | list[ThreadDict],
    step_id: str,
) -> dict[str, Any] | None:
    for step in steps:
        if str(step.get("id") or "") == step_id:
            return step
        nested_steps = step.get("steps") or []
        if nested_steps:
            found = find_step_by_id(nested_steps, step_id)
            if found is not None:
                return found
    return None


def generation_is_terminal(status: str | None) -> bool:
    return status in {"stopped", "completed", "done", "error", "failed"}


def apply_generation_overlay_to_step(
    step: dict[str, Any],
    entry: ActiveGeneration,
) -> bool:
    metadata = parse_step_metadata(step)
    existing_output = str(step.get("output") or "")
    next_output = entry.partial_output
    next_status = entry.generation_status
    updated = False

    if len(next_output) >= len(existing_output) and next_output != existing_output:
        step["output"] = next_output
        updated = True

    next_streaming = not generation_is_terminal(next_status)
    if bool(step.get("streaming")) != next_streaming:
        step["streaming"] = next_streaming
        updated = True

    if metadata.get("generation_status") != next_status:
        if next_status:
            metadata["generation_status"] = next_status
        else:
            metadata.pop("generation_status", None)
        updated = True

    if updated:
        step["metadata"] = metadata or None
    return updated


async def persist_step_snapshot(step_dict: dict[str, Any]) -> None:
    data_layer = get_data_layer()
    if not data_layer:
        return

    try:
        await data_layer.update_step(step_dict)
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning(
            "Failed to persist step snapshot for %s: %s",
            step_dict.get("id"),
            exc,
        )

    thread_id = str(step_dict.get("threadId") or "")
    step_id = str(step_dict.get("id") or "")
    if thread_id and step_id:
        entry = await GENERATION_REGISTRY.get(thread_id)
        if entry is not None and entry.assistant_message_id == step_id:
            await GENERATION_REGISTRY.mark_snapshot(thread_id)


async def persist_message_snapshot(message: cl.Message) -> None:
    await persist_step_snapshot(message.to_dict())


async def emit_message_placeholder(message: cl.Message) -> None:
    try:
        await message.send()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning(
            "Failed to send message placeholder for %s: %s", message.id, exc
        )
        await persist_message_snapshot(message)


async def emit_message_update(message: cl.Message) -> None:
    try:
        await message.update()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning("Failed to update message %s: %s", message.id, exc)
        await persist_message_snapshot(message)


async def enqueue_thread_metadata_patch(
    thread_id: str,
    patch: Mapping[str, Any],
) -> None:
    await THREAD_METADATA_BUFFER.enqueue(thread_id, patch)


async def persist_request_metadata(request_id: str | None, model_name: str) -> None:
    metadata = context.session.to_persistable()
    metadata["selected_model"] = model_name
    metadata["chainlit_thread_id"] = context.session.thread_id
    metadata["chainlit_user_identifier"] = current_user_identifier()
    if request_id:
        metadata["last_request_id"] = request_id

    await enqueue_thread_metadata_patch(
        context.session.thread_id,
        metadata,
    )


async def hydrate_thread_from_generation_registry(thread: ThreadDict) -> None:
    thread_id = str(thread.get("id") or "")
    if not thread_id:
        return

    entry = await GENERATION_REGISTRY.get(thread_id)
    if entry is None:
        return

    steps = thread.get("steps") or []
    step = find_step_by_id(steps, entry.assistant_message_id)
    registry_is_newer = False

    if step is None:
        step = {
            "id": entry.assistant_message_id,
            "threadId": thread_id,
            "name": "Assistant",
            "type": "assistant_message",
            "output": entry.partial_output,
            "createdAt": entry.updated_at,
            "streaming": not generation_is_terminal(entry.generation_status),
            "metadata": message_metadata_with_status(entry.generation_status),
        }
        steps.append(step)
        thread["steps"] = steps
        registry_is_newer = True
    else:
        registry_is_newer = apply_generation_overlay_to_step(step, entry)

    if registry_is_newer:
        await persist_step_snapshot(step)


def build_team_user(game_id: int, team_id: int) -> User:
    return User(
        identifier=f"gzctf-team:{game_id}:{team_id}",
        display_name=f"Team #{team_id}",
        metadata={
            "role": "gzctf-team",
            "auth_source": "team-token",
            "display_name": f"Team #{team_id}",
            "game_id": game_id,
            "team_id": team_id,
        },
    )


async def ensure_team_token_subject(user: User) -> dict[str, Any] | None:
    managed_user = await CONTROL_PLANE_REPOSITORY.get_managed_user(
        user.identifier,
        include_deleted=True,
    )
    if managed_user is not None:
        return managed_user

    await CONTROL_PLANE_REPOSITORY.create_managed_user(
        identifier=user.identifier,
        display_name=user.display_name or user.identifier,
        password_hash=TEAM_TOKEN_AUTH_PASSWORD_HASH,
        is_active=True,
    )
    return await CONTROL_PLANE_REPOSITORY.get_managed_user(
        user.identifier,
        include_deleted=True,
    )


@cl.password_auth_callback
async def password_auth_callback(username: str, password: str) -> User | None:
    if team_token_auth_enabled():
        return None

    managed_user = await CONTROL_PLANE_REPOSITORY.get_managed_user(username)
    if (
        managed_user
        and managed_user.get("is_active")
        and not managed_user.get("is_deleted")
        and verify_password(password, managed_user.get("password_hash", ""))
    ):
        await CONTROL_PLANE_REPOSITORY.touch_managed_user_login(username)
        APP_METRICS.record_auth_attempt("password", "success")
        return User(
            identifier=username,
            display_name=managed_user.get("display_name") or username,
            metadata={"role": "managed-user"},
        )
    APP_METRICS.record_auth_attempt("password", "failure")
    return None


CurrentUserParam = PersistedUser | User | None


def chainlit_api_paths(path: str) -> set[str]:
    normalized_path = path if path.startswith("/") else f"/{path}"
    root_path = (config.run.root_path or SETTINGS.chainlit_root_path).rstrip("/")
    paths = {normalized_path}
    if root_path:
        paths.add(f"{root_path}{normalized_path}")
    return paths


def prioritize_chainlit_routes(paths: set[str]) -> None:
    prioritized_routes = [
        route
        for route in chainlit_app.router.routes
        if getattr(route, "path", None) in paths
    ]
    remaining_routes = [
        route
        for route in chainlit_app.router.routes
        if getattr(route, "path", None) not in paths
    ]
    chainlit_app.router.routes = prioritized_routes + remaining_routes


def chainlit_route_exists(path: str, method: str) -> bool:
    expected_method = method.upper()
    for route in chainlit_app.router.routes:
        if getattr(route, "path", None) != path:
            continue
        methods = getattr(route, "methods", set()) or set()
        if expected_method in methods:
            return True
    return False


def render_team_token_login_page(next_path: str | None = None) -> str:
    login_endpoint = html.escape(
        chainlit_path("/api/team-token/login"),
        quote=True,
    )
    home_path = html.escape(chainlit_home_path(), quote=True)
    safe_next_path = sanitize_next_path(next_path, fallback=chainlit_home_path())

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>请输入您在CTF平台中的Team token来进入对话</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f4efe4;
        --card: rgba(255, 251, 243, 0.95);
        --border: rgba(117, 90, 48, 0.18);
        --text: #2d2416;
        --muted: #6f6048;
        --accent: #946319;
        --accent-strong: #6f4912;
        --error: #b42318;
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        min-height: 100vh;
        font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top, rgba(246, 204, 92, 0.28), transparent 38%),
          linear-gradient(160deg, #f6f0e3 0%, #efe4d0 48%, #e6d9c3 100%);
        display: grid;
        place-items: center;
        padding: 24px;
      }}

      .shell {{
        width: min(100%, 460px);
        border: 1px solid var(--border);
        background: var(--card);
        backdrop-filter: blur(14px);
        border-radius: 24px;
        padding: 32px;
        box-shadow: 0 28px 70px rgba(85, 57, 15, 0.16);
      }}

      .eyebrow {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        color: var(--muted);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}

      h1 {{
        margin: 14px 0 10px;
        font-family: "IBM Plex Serif", "Noto Serif SC", serif;
        font-size: clamp(28px, 5vw, 38px);
        line-height: 1.1;
      }}

      p {{
        margin: 0;
        color: var(--muted);
        line-height: 1.65;
      }}

      form {{
        margin-top: 26px;
      }}

      label {{
        display: block;
        margin-bottom: 10px;
        font-size: 14px;
        font-weight: 600;
      }}

      .visually-hidden {{
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }}

      textarea {{
        width: 100%;
        min-height: 156px;
        resize: vertical;
        border-radius: 16px;
        border: 1px solid rgba(117, 90, 48, 0.22);
        background: rgba(255, 255, 255, 0.88);
        color: var(--text);
        font: inherit;
        padding: 16px 18px;
        box-shadow: inset 0 1px 2px rgba(45, 36, 22, 0.08);
      }}

      textarea:focus {{
        outline: 2px solid rgba(148, 99, 25, 0.2);
        border-color: rgba(148, 99, 25, 0.5);
      }}

      .hint {{
        margin-top: 10px;
        font-size: 13px;
      }}

      button {{
        margin-top: 18px;
        width: 100%;
        border: 0;
        border-radius: 999px;
        padding: 14px 18px;
        font: inherit;
        font-weight: 700;
        color: #fff;
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
        cursor: pointer;
        transition: transform 0.18s ease, box-shadow 0.18s ease;
        box-shadow: 0 16px 32px rgba(111, 73, 18, 0.18);
      }}

      button:hover {{
        transform: translateY(-1px);
      }}

      button:disabled {{
        cursor: wait;
        opacity: 0.8;
        transform: none;
      }}

      .status {{
        min-height: 24px;
        margin-top: 16px;
        font-size: 14px;
      }}

      .status.error {{
        color: var(--error);
      }}

      .status.success {{
        color: var(--accent-strong);
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <h1>请输入您在CTF平台中的Team token来进入对话</h1>
      <form id="team-token-form">
        <label class="visually-hidden" for="team-token">Team Token</label>
        <textarea
          id="team-token"
          name="teamToken"
          autocomplete="off"
          autocorrect="off"
          autocapitalize="none"
          spellcheck="false"
          aria-label="Team Token"
          required
        ></textarea>
        <button id="submit-button" type="submit">进入 Chat</button>
        <div id="status" class="status" aria-live="polite"></div>
      </form>
    </main>
    <script>
      const form = document.getElementById("team-token-form");
      const tokenInput = document.getElementById("team-token");
      const statusNode = document.getElementById("status");
      const submitButton = document.getElementById("submit-button");
      const loginEndpoint = {json.dumps(login_endpoint)};
      const homePath = {json.dumps(home_path)};
      const nextPath = {json.dumps(safe_next_path)};

      const setStatus = (message, tone) => {{
        statusNode.textContent = message || "";
        statusNode.className = tone ? `status ${{tone}}` : "status";
      }};

      form.addEventListener("submit", async (event) => {{
        event.preventDefault();
        const teamToken = tokenInput.value.trim();
        if (!teamToken) {{
          setStatus("请输入有效的 Team Token。", "error");
          tokenInput.focus();
          return;
        }}

        submitButton.disabled = true;
        setStatus("正在验证 teamToken ...");

        try {{
          const response = await fetch(loginEndpoint, {{
            method: "POST",
            headers: {{
              "Content-Type": "application/json"
            }},
            credentials: "same-origin",
            body: JSON.stringify({{
              teamToken,
              next: nextPath
            }})
          }});
          const payload = await response.json().catch(() => ({{}}));
          if (!response.ok) {{
            setStatus(payload.detail || "登录失败，请检查 token。", "error");
            return;
          }}

          setStatus("验证成功，正在进入 Chat ...", "success");
          window.location.href = payload.redirectTo || homePath;
        }} catch (error) {{
          setStatus("登录请求失败，请稍后重试。", "error");
        }} finally {{
          submitButton.disabled = false;
        }}
      }});
    </script>
  </body>
</html>"""


async def get_team_token_login_page(request: Request) -> HTMLResponse:
    if not team_token_auth_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    next_path = sanitize_next_path(
        request.query_params.get("next"),
        fallback=chainlit_home_path(),
    )
    return HTMLResponse(render_team_token_login_page(next_path))


async def post_team_token_login(request: Request) -> JSONResponse:
    if not team_token_auth_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse(
            {"detail": "request body must be valid JSON"},
            status_code=400,
        )

    if not isinstance(payload, dict):
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse({"detail": "request body must be an object"}, status_code=400)

    raw_team_token = payload.get("teamToken")
    if not isinstance(raw_team_token, str):
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse({"detail": "teamToken must be a string"}, status_code=400)

    redirect_to = sanitize_next_path(
        payload.get("next"),
        fallback=chainlit_home_path(),
    )

    try:
        parsed_team_token = parse_team_token(raw_team_token)
    except ValueError as exc:
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse({"detail": str(exc)}, status_code=400)

    try:
        team_token_auth_config = await get_team_token_auth_config()
    except ValueError as exc:
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse({"detail": str(exc)}, status_code=503)

    try:
        token_verified = verify_gzctf_team_token(
            parsed_team_token.team_id,
            parsed_team_token.signature,
            str(team_token_auth_config.get("public_key") or ""),
        )
    except ValueError:
        token_verified = False

    if not token_verified:
        APP_METRICS.record_auth_attempt("team-token", "failure")
        return JSONResponse({"detail": "invalid team token"}, status_code=401)

    user = build_team_user(fixed_game_id(), parsed_team_token.team_id)
    user.metadata[TEAM_TOKEN_AUTH_VERSION_METADATA_KEY] = parse_config_version(
        team_token_auth_config.get("config_version")
    )
    managed_user = await ensure_team_token_subject(user)
    if managed_user is not None and not is_managed_user_active(managed_user):
        APP_METRICS.record_auth_attempt("team-token", "disabled")
        return JSONResponse({"detail": "current team is disabled"}, status_code=403)

    data_layer = get_data_layer()
    if data_layer:
        try:
            await data_layer.create_user(user)
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning(
                "Failed to persist team-token user %s: %s",
                user.identifier,
                exc,
            )

    access_token = create_jwt(user)
    response = JSONResponse(
        {
            "success": True,
            "redirectTo": redirect_to,
            "user": {
                "identifier": user.identifier,
                "display_name": user.display_name,
            },
        }
    )
    set_auth_cookie(request, response, access_token)
    APP_METRICS.record_auth_attempt("team-token", "success")
    return response


async def get_metrics_response() -> Response:
    return APP_METRICS.render_metrics()


def configure_team_token_auth_mode() -> None:
    if not team_token_auth_enabled():
        return

    ensure_team_token_auth_config()
    os.environ["CHAINLIT_CUSTOM_AUTH"] = "1"
    config.code.password_auth_callback = None

    login_paths = chainlit_api_paths("/login")
    login_api_paths = chainlit_api_paths("/api/team-token/login")

    for login_path in login_paths:
        if not chainlit_route_exists(login_path, "GET"):
            chainlit_app.add_api_route(
                login_path,
                get_team_token_login_page,
                methods=["GET"],
                response_class=HTMLResponse,
            )

    for login_api_path in login_api_paths:
        if not chainlit_route_exists(login_api_path, "POST"):
            chainlit_app.add_api_route(
                login_api_path,
                post_team_token_login,
                methods=["POST"],
            )

    prioritize_chainlit_routes(login_paths | login_api_paths)


def configure_metrics_routes() -> None:
    metrics_paths = chainlit_api_paths("/metrics")
    for metrics_path in metrics_paths:
        if not chainlit_route_exists(metrics_path, "GET"):
            chainlit_app.add_api_route(
                metrics_path,
                get_metrics_response,
                methods=["GET"],
            )
    prioritize_chainlit_routes(metrics_paths)


async def listen_for_team_token_auth_updates() -> None:
    global TEAM_TOKEN_AUTH_LAST_VERSION

    while True:
        redis: Redis | None = None
        pubsub = None
        try:
            redis = Redis.from_url(SETTINGS.redis.url, decode_responses=True)
            await redis.ping()
            pubsub = redis.pubsub()
            await pubsub.subscribe(TEAM_TOKEN_AUTH_EVENTS_CHANNEL)

            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    await asyncio.sleep(0.1)
                    continue
                if message.get("type") != "message":
                    continue

                event = parse_team_token_auth_event(message.get("data") or "")
                if event.get("event") != TEAM_TOKEN_AUTH_EVENT_PUBLIC_KEY_UPDATED:
                    continue

                next_version = parse_config_version(event.get("config_version"))
                if next_version <= TEAM_TOKEN_AUTH_LAST_VERSION:
                    continue

                TEAM_TOKEN_AUTH_LAST_VERSION = next_version
                await sio.emit("reload", {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - operational path
            LOGGER.warning(
                "Team token auth update listener failed, retrying: %s",
                exc,
            )
            await asyncio.sleep(1)
        finally:
            if pubsub is not None:
                await pubsub.close()
            if redis is not None:
                await redis.close()


async def startup_team_token_auth_runtime() -> None:
    global TEAM_TOKEN_AUTH_EVENTS_TASK

    if not team_token_auth_enabled():
        return

    try:
        await get_team_token_auth_config()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.warning("Team token auth config preload failed: %s", exc)

    if TEAM_TOKEN_AUTH_EVENTS_TASK is None or TEAM_TOKEN_AUTH_EVENTS_TASK.done():
        TEAM_TOKEN_AUTH_EVENTS_TASK = asyncio.create_task(
            listen_for_team_token_auth_updates()
        )


def build_caps_ftp_runtime() -> CapsFTPServerRuntime:
    storage_root = caps_game_root()
    return CapsFTPServerRuntime(
        host=SETTINGS.caps.ftp_host,
        port=SETTINGS.caps.ftp_port,
        username=SETTINGS.caps.ftp_username,
        password=SETTINGS.caps.ftp_password,
        storage_root=storage_root,
        passive_ports=SETTINGS.caps.passive_ports,
        public_host=SETTINGS.caps.ftp_public_host,
        tls_enabled=SETTINGS.caps.ftp_tls_enabled,
        tls_certfile=SETTINGS.caps.ftp_tls_certfile,
        tls_keyfile=SETTINGS.caps.ftp_tls_keyfile,
        tls_control_required=SETTINGS.caps.ftp_tls_control_required,
        tls_data_required=SETTINGS.caps.ftp_tls_data_required,
    )


async def startup_caps_runtime() -> None:
    global CAPS_FTP_RUNTIME

    if not caps_enabled():
        return
    if SETTINGS.runtime.worker_index != 1:
        LOGGER.info(
            "Skipping CAPS FTP runtime on %s; worker 1 owns the shared listener",
            CHAINLIT_WORKER_ID,
        )
        return

    ensure_caps_storage_root_ready(caps_game_root())
    runtime = build_caps_ftp_runtime()
    runtime.start()
    CAPS_FTP_RUNTIME = runtime


async def shutdown_caps_runtime() -> None:
    global CAPS_FTP_RUNTIME

    if CAPS_FTP_RUNTIME is not None:
        CAPS_FTP_RUNTIME.stop()
        CAPS_FTP_RUNTIME = None


async def shutdown_team_token_auth_runtime() -> None:
    global TEAM_TOKEN_AUTH_CACHE_REDIS, TEAM_TOKEN_AUTH_EVENTS_TASK

    if TEAM_TOKEN_AUTH_EVENTS_TASK is not None:
        TEAM_TOKEN_AUTH_EVENTS_TASK.cancel()
        try:
            await TEAM_TOKEN_AUTH_EVENTS_TASK
        except asyncio.CancelledError:
            pass
        TEAM_TOKEN_AUTH_EVENTS_TASK = None

    if TEAM_TOKEN_AUTH_CACHE_REDIS is not None:
        await TEAM_TOKEN_AUTH_CACHE_REDIS.close()
        TEAM_TOKEN_AUTH_CACHE_REDIS = None


async def shutdown_application_runtime() -> None:
    try:
        await THREAD_METADATA_BUFFER.close()
    finally:
        try:
            await GLOBAL_ADMISSION_CONTROLLER.close()
        finally:
            try:
                await DATA_LAYER.close()
            finally:
                try:
                    await QUOTA_SERVICE.close()
                finally:
                    try:
                        await close_async_resource(
                            LITELLM_CLIENT,
                            label="LiteLLM shared client",
                        )
                        await close_async_resource(
                            LITELLM_HTTP_CLIENT,
                            label="LiteLLM shared httpx client",
                        )
                    finally:
                        await CONTROL_PLANE_ENGINE.dispose()


async def chainlit_app_startup() -> None:
    QUOTA_SERVICE.set_thread_metadata_callback(enqueue_thread_metadata_patch)
    await GLOBAL_ADMISSION_CONTROLLER.initialize()
    await THREAD_METADATA_BUFFER.start()
    await startup_team_token_auth_runtime()


async def chainlit_app_shutdown() -> None:
    try:
        await shutdown_team_token_auth_runtime()
    finally:
        await shutdown_application_runtime()


async def get_my_quota(
    current_user: CurrentUserParam = Depends(get_current_user),
) -> dict[str, Any]:
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    summary = await QUOTA_SERVICE.get_user_quota(current_user.identifier)
    return {
        "user_identifier": current_user.identifier,
        "quota": summary.to_api_dict(),
    }


quota_paths = chainlit_api_paths("/api/quota/me")

for quota_path in quota_paths:
    chainlit_app.add_api_route(quota_path, get_my_quota, methods=["GET"])

prioritize_chainlit_routes(quota_paths)
chainlit_app.dependency_overrides[get_current_user] = (
    get_current_user_with_team_token_version_guard
)
chainlit_socket.get_current_user = get_current_user_with_team_token_version_guard
config.code.on_app_startup = chainlit_app_startup
config.code.on_app_shutdown = chainlit_app_shutdown
configure_team_token_auth_mode()
configure_metrics_routes()


if thinking_enabled():

    @cl.set_chat_profiles
    async def set_chat_profiles(
        current_user: User | None, language: str | None
    ) -> list[ChatProfile]:
        del current_user, language
        return [
            ChatProfile(
                name=SETTINGS.models.default_model,
                display_name=SETTINGS.models.default_model_display_name,
                markdown_description="Default low-friction chat profile.",
                default=True,
            ),
            ChatProfile(
                name=SETTINGS.models.thinking_model,
                display_name=SETTINGS.models.thinking_model_display_name,
                markdown_description="Optional thinking-enabled profile.",
            ),
        ]


@cl.on_chat_start
async def on_chat_start() -> None:
    if not await ensure_active_chat_session_allowed():
        return
    cl.user_session.set("selected_model", resolve_model_alias())
    cl.user_session.set("last_request_id", None)


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    if not await ensure_active_chat_session_allowed():
        return
    await hydrate_thread_from_generation_registry(thread)
    metadata = parse_thread_metadata(thread)
    selected_model = metadata.get("selected_model")
    if selected_model not in available_models():
        selected_model = resolve_model_alias()

    cl.user_session.set("selected_model", selected_model)
    cl.user_session.set("last_request_id", metadata.get("last_request_id"))


@cl.on_stop
async def on_stop() -> None:
    task = await GENERATION_REGISTRY.cancel_for_user(
        context.session.thread_id, current_user_identifier()
    )
    if task is not None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass


@cl.on_message
async def on_message(message: cl.Message) -> None:
    del message

    if not await ensure_active_chat_session_allowed():
        return

    model_name = resolve_model_alias()
    user_identifier = current_user_identifier()
    thread_id = context.session.thread_id
    reservation = None
    provider_request_id: str | None = None
    usage = None
    snapshot_interval_seconds = SETTINGS.runtime.stream_snapshot_interval_seconds
    stream_delivery_failed = False
    client: AsyncOpenAI | None = None
    stream = None
    assistant_placeholder_sent = False
    global_admission_lease: AdmissionLease | None = None
    litellm_slot_acquired = False
    queue_notice_shown = False
    queue_wait_seconds = 0.0

    cl.user_session.set("selected_model", model_name)

    assistant_message = cl.Message(
        content="",
        id=active_assistant_message_id(),
        metadata=message_metadata_with_status("pending"),
    )
    llm_messages = [
        {"role": "system", "content": SETTINGS.models.system_prompt},
        *cl.chat_context.to_openai(),
    ]

    try:
        await emit_message_placeholder(assistant_message)
        assistant_placeholder_sent = True
        await GENERATION_REGISTRY.start(
            thread_id=thread_id,
            assistant_message_id=assistant_message.id,
            partial_output="",
            generation_status="pending",
            user_identifier=user_identifier,
            task=asyncio.current_task(),
        )

        client = build_client()
        queue_wait_expected = LITELLM_REQUEST_SEMAPHORE.locked()
        if not queue_wait_expected:
            queue_wait_expected = await GLOBAL_ADMISSION_CONTROLLER.is_saturated()
        if queue_wait_expected:
            assistant_message.metadata = message_metadata_with_status("queued")
            assistant_message.content = "系统繁忙，正在排队，请稍候..."
            await GENERATION_REGISTRY.set_status(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                generation_status="queued",
            )
            await emit_message_update(assistant_message)
            queue_notice_shown = True

        global_admission_lease, queue_wait_seconds = (
            await GLOBAL_ADMISSION_CONTROLLER.acquire()
        )
        if global_admission_lease is None:
            raise UpstreamQueueTimeoutError("当前请求较多，排队等待超时，请稍后重试。")

        litellm_slot_acquired, local_wait_seconds = await acquire_litellm_request_slot()
        queue_wait_seconds += local_wait_seconds
        if not litellm_slot_acquired:
            raise UpstreamQueueTimeoutError("当前请求较多，排队等待超时，请稍后重试。")
        if queue_notice_shown:
            assistant_message.content = ""
            assistant_message.metadata = message_metadata_with_status("pending")
            await GENERATION_REGISTRY.set_status(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                generation_status="pending",
            )
            await emit_message_update(assistant_message)
            LOGGER.info(
                "Upstream slot acquired after queue wait %.2fs (user=%s thread=%s)",
                queue_wait_seconds,
                user_identifier,
                thread_id,
            )
        reservation = await QUOTA_SERVICE.reserve(
            thread_id=thread_id,
            user_identifier=user_identifier,
            step_id=assistant_message.id,
            messages=llm_messages,
        )
        stream = await client.chat.completions.create(
            model=model_name,
            messages=llm_messages,
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=reservation.allowed_completion_tokens,
            user=user_identifier,
            extra_body={
                "metadata": {
                    "app": "ncoai-chainlit",
                    "chainlit_thread_id": thread_id,
                    "chainlit_user_identifier": user_identifier,
                    "selected_model": model_name,
                    "quota_request_id": reservation.request_id,
                }
            },
        )

        async for part in stream:
            if not provider_request_id and getattr(part, "id", None):
                provider_request_id = part.id

            if getattr(part, "usage", None) is not None:
                usage = part.usage

            if not part.choices:
                continue

            token = part.choices[0].delta.content or ""
            if token:
                assistant_message.metadata = clear_generation_status(
                    assistant_message.metadata
                )
                await GENERATION_REGISTRY.append_token(
                    thread_id=thread_id,
                    assistant_message_id=assistant_message.id,
                    token=token,
                    generation_status="streaming",
                )
                try:
                    await assistant_message.stream_token(token)
                except Exception as exc:  # pragma: no cover - operational path
                    if not stream_delivery_failed:
                        LOGGER.warning(
                            "Streaming token delivery failed for %s: %s",
                            assistant_message.id,
                            exc,
                        )
                        stream_delivery_failed = True
                if await GENERATION_REGISTRY.snapshot_due(
                    thread_id=thread_id,
                    assistant_message_id=assistant_message.id,
                    interval_seconds=snapshot_interval_seconds,
                ):
                    await persist_message_snapshot(assistant_message)

        settlement = await QUOTA_SERVICE.settle(
            reservation=reservation,
            provider_request_id=provider_request_id,
            assistant_content=assistant_message.content,
            usage=usage,
        )
        APP_METRICS.record_upstream_request("litellm", "success")
        assistant_message.streaming = False
        assistant_message.metadata = message_metadata_with_status(
            None,
            token_usage=settlement.to_message_metadata(),
        )
        if provider_request_id:
            cl.user_session.set("last_request_id", provider_request_id)
        await persist_request_metadata(provider_request_id, model_name)
        await persist_message_snapshot(assistant_message)
        await emit_message_update(assistant_message)
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task and hasattr(current_task, "uncancel"):
            current_task.uncancel()

        await close_async_resource(stream, label="LLM stream")
        stream = None

        should_settle = bool(
            reservation
            and (
                provider_request_id
                or assistant_message.content
                or usage is not None
            )
        )

        token_usage_metadata = None
        if reservation and should_settle:
            settlement = await QUOTA_SERVICE.settle(
                reservation=reservation,
                provider_request_id=provider_request_id,
                assistant_content=assistant_message.content,
                usage=usage,
            )
            token_usage_metadata = settlement.to_message_metadata()
            if provider_request_id:
                cl.user_session.set("last_request_id", provider_request_id)
        elif reservation:
            await QUOTA_SERVICE.release(reservation, status="cancelled")
        APP_METRICS.record_upstream_request("litellm", "cancelled")

        if reservation:
            await persist_request_metadata(provider_request_id, model_name)

        if assistant_placeholder_sent:
            assistant_message.streaming = False
            assistant_message.metadata = message_metadata_with_status(
                "stopped",
                token_usage=token_usage_metadata,
            )
            await GENERATION_REGISTRY.set_content(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                partial_output=assistant_message.content,
                generation_status="stopped",
            )
            await persist_message_snapshot(assistant_message)
            await emit_message_update(assistant_message)
        return
    except QuotaExceededError as exc:
        APP_METRICS.record_quota_event(exc.scope, "blocked")
        thread_summary = exc.thread_summary
        if exc.scope == "thread":
            thread_summary = await QUOTA_SERVICE.mark_thread_blocked(
                thread_id=thread_id,
                summary=exc.thread_summary,
                message=str(exc),
            )
        quota_metadata = quota_message_metadata(
            exc.scope,
            str(exc),
            thread_summary.to_metadata_summary(),
            exc.user_summary.to_api_dict(),
        )
        if assistant_placeholder_sent:
            assistant_message.streaming = False
            assistant_message.content = str(exc)
            assistant_message.metadata = quota_metadata
            await GENERATION_REGISTRY.set_content(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                partial_output=assistant_message.content,
                generation_status="stopped",
            )
            await persist_message_snapshot(assistant_message)
            await emit_message_update(assistant_message)
        else:
            await cl.Message(
                content=str(exc),
                metadata=quota_metadata,
            ).send()
    except UpstreamQueueTimeoutError as exc:
        APP_METRICS.record_upstream_request("litellm", "queue_timeout")
        if assistant_placeholder_sent:
            assistant_message.is_error = True
            assistant_message.streaming = False
            assistant_message.metadata = clear_generation_status(
                assistant_message.metadata
            )
            assistant_message.content = str(exc)
            await GENERATION_REGISTRY.set_content(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                partial_output=assistant_message.content,
                generation_status="stopped",
            )
            await persist_message_snapshot(assistant_message)
            await emit_message_update(assistant_message)
        else:
            await cl.Message(content=str(exc)).send()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.exception("LiteLLM chat request failed")
        APP_METRICS.record_upstream_request(
            "litellm",
            classify_exception_result(exc),
        )
        error_message = f"Chat request failed: {exc}"
        if reservation and (
            provider_request_id or assistant_message.content or usage is not None
        ):
            settlement = await QUOTA_SERVICE.settle(
                reservation=reservation,
                provider_request_id=provider_request_id,
                assistant_content=assistant_message.content,
                usage=usage,
            )
            assistant_message.metadata = message_metadata_with_status(
                None,
                token_usage=settlement.to_message_metadata(),
            )
            assistant_message.is_error = True
            assistant_message.content = (
                f"{assistant_message.content}\n\n{error_message}"
                if assistant_message.content
                else error_message
            )
            if provider_request_id:
                cl.user_session.set("last_request_id", provider_request_id)
            await persist_request_metadata(provider_request_id, model_name)
            await GENERATION_REGISTRY.set_content(
                thread_id=thread_id,
                assistant_message_id=assistant_message.id,
                partial_output=assistant_message.content,
                generation_status="stopped",
            )
            await persist_message_snapshot(assistant_message)
            await emit_message_update(assistant_message)
        else:
            if reservation:
                await QUOTA_SERVICE.release(reservation, status="released")
                await persist_request_metadata(provider_request_id, model_name)
            if assistant_placeholder_sent:
                assistant_message.is_error = True
                assistant_message.streaming = False
                assistant_message.metadata = clear_generation_status(
                    assistant_message.metadata
                )
                assistant_message.content = error_message
                await GENERATION_REGISTRY.set_content(
                    thread_id=thread_id,
                    assistant_message_id=assistant_message.id,
                    partial_output=assistant_message.content,
                    generation_status="stopped",
                )
                await persist_message_snapshot(assistant_message)
                await emit_message_update(assistant_message)
            else:
                await cl.Message(content=error_message).send()
    finally:
        await close_async_resource(stream, label="LLM stream")
        if litellm_slot_acquired:
            LITELLM_REQUEST_SEMAPHORE.release()
        await GLOBAL_ADMISSION_CONTROLLER.release(global_admission_lease)
        await GENERATION_REGISTRY.remove(
            thread_id=thread_id,
            assistant_message_id=assistant_message.id,
        )
