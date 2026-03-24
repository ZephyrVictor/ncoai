from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import chainlit as cl
from fastapi import Depends, HTTPException
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.context import context
from chainlit.data import get_data_layer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.server import app as chainlit_app
from chainlit.types import ChatProfile, ThreadDict
from chainlit.user import PersistedUser, User
from openai import AsyncOpenAI

from database import ensure_chainlit_schema
from quota import QuotaExceededError, QuotaService
from settings import SETTINGS

LOGGER = logging.getLogger("ncoai.chainlit")
asyncio.run(ensure_chainlit_schema(SETTINGS.database.async_url))
DATA_LAYER = SQLAlchemyDataLayer(SETTINGS.database.async_url, storage_provider=None)
QUOTA_SERVICE = QuotaService(SETTINGS.database.async_url, settings=SETTINGS)

try:
    asyncio.run(QUOTA_SERVICE.initialize())
except Exception as exc:  # pragma: no cover - operational path
    LOGGER.exception("Quota service initialization failed: %s", exc)


@cl.data_layer
def chainlit_data_layer() -> SQLAlchemyDataLayer:
    return DATA_LAYER


def thinking_enabled() -> bool:
    return SETTINGS.models.thinking_enabled


def current_user_identifier() -> str:
    session_user = getattr(context.session, "user", None)
    if session_user and getattr(session_user, "identifier", None):
        return session_user.identifier
    user = cl.user_session.get("user")
    if user and getattr(user, "identifier", None):
        return user.identifier
    return "anonymous"


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
    return AsyncOpenAI(
        base_url=SETTINGS.litellm.base_url,
        api_key=SETTINGS.litellm.api_key,
        timeout=SETTINGS.litellm.timeout_seconds,
    )


def quota_message_metadata(
    scope: str | None,
    thread_summary: dict[str, Any],
    user_summary: dict[str, Any],
) -> dict[str, Any]:
    updated_at = thread_summary.get("updated_at") or user_summary.get("updated_at")
    return {
        "quota_status": {"blocked_scope": scope},
        "token_usage": {
            "request_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "session_used_tokens": thread_summary.get("used_tokens", 0),
            "session_limit_tokens": thread_summary.get("limit_tokens", 0),
            "user_used_tokens": user_summary.get("used_tokens", 0),
            "user_limit_tokens": user_summary.get("limit_tokens", 0),
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


async def persist_request_metadata(request_id: str | None, model_name: str) -> None:
    data_layer = get_data_layer()
    if not data_layer:
        return

    metadata = context.session.to_persistable()
    metadata["selected_model"] = model_name
    metadata["chainlit_thread_id"] = context.session.thread_id
    metadata["chainlit_user_identifier"] = current_user_identifier()
    if request_id:
        metadata["last_request_id"] = request_id

    await data_layer.update_thread(
        thread_id=context.session.thread_id,
        metadata=metadata,
    )


@cl.password_auth_callback
async def password_auth_callback(username: str, password: str) -> User | None:
    if username == SETTINGS.auth.username and password == SETTINGS.auth.password:
        return User(
            identifier=username,
            display_name=username,
            metadata={"role": "acceptance-user"},
        )
    return None


CurrentUserParam = PersistedUser | User | None


def chainlit_api_paths(path: str) -> set[str]:
    normalized_path = path if path.startswith("/") else f"/{path}"
    root_path = (config.run.root_path or SETTINGS.chainlit_root_path).rstrip("/")
    paths = {normalized_path}
    if root_path:
        paths.add(f"{root_path}{normalized_path}")
    return paths


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

quota_routes = [
    route
    for route in chainlit_app.router.routes
    if getattr(route, "path", None) in quota_paths
]
non_quota_routes = [
    route
    for route in chainlit_app.router.routes
    if getattr(route, "path", None) not in quota_paths
]
chainlit_app.router.routes = quota_routes + non_quota_routes


if thinking_enabled():

    @cl.set_chat_profiles
    async def set_chat_profiles(
        current_user: User | None, language: str | None
    ) -> list[ChatProfile]:
        del current_user, language
        return [
            ChatProfile(
                name=SETTINGS.models.default_model,
                display_name="Qwen3.5-35B",
                markdown_description="Default low-friction chat profile.",
                default=True,
            ),
            ChatProfile(
                name=SETTINGS.models.thinking_model,
                display_name="Qwen3.5-35B Thinking",
                markdown_description="Optional thinking-enabled profile.",
            ),
        ]


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("selected_model", resolve_model_alias())
    cl.user_session.set("last_request_id", None)


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    metadata = parse_thread_metadata(thread)
    selected_model = metadata.get("selected_model")
    if selected_model not in available_models():
        selected_model = resolve_model_alias()

    cl.user_session.set("selected_model", selected_model)
    cl.user_session.set("last_request_id", metadata.get("last_request_id"))


@cl.on_message
async def on_message(message: cl.Message) -> None:
    del message

    model_name = resolve_model_alias()
    user_identifier = current_user_identifier()
    thread_id = context.session.thread_id
    reservation = None
    provider_request_id: str | None = None
    usage = None

    cl.user_session.set("selected_model", model_name)

    assistant_message = cl.Message(content="")
    llm_messages = [
        {"role": "system", "content": SETTINGS.models.system_prompt},
        *cl.chat_context.to_openai(),
    ]

    try:
        reservation = await QUOTA_SERVICE.reserve(
            thread_id=thread_id,
            user_identifier=user_identifier,
            step_id=assistant_message.id,
            messages=llm_messages,
        )

        stream = await build_client().chat.completions.create(
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
                await assistant_message.stream_token(token)

        settlement = await QUOTA_SERVICE.settle(
            reservation=reservation,
            provider_request_id=provider_request_id,
            assistant_content=assistant_message.content,
            usage=usage,
        )
        assistant_message.metadata = {"token_usage": settlement.to_message_metadata()}
        if provider_request_id:
            cl.user_session.set("last_request_id", provider_request_id)
        await persist_request_metadata(provider_request_id, model_name)
        await assistant_message.update()
    except QuotaExceededError as exc:
        await cl.Message(
            content=str(exc),
            metadata=quota_message_metadata(
                exc.scope,
                exc.thread_summary.to_metadata_summary(),
                exc.user_summary.to_api_dict(),
            ),
        ).send()
    except Exception as exc:  # pragma: no cover - operational path
        LOGGER.exception("LiteLLM chat request failed")
        error_message = f"Chat request failed: {exc}"
        if reservation and assistant_message.content:
            settlement = await QUOTA_SERVICE.settle(
                reservation=reservation,
                provider_request_id=provider_request_id,
                assistant_content=assistant_message.content,
                usage=usage,
            )
            assistant_message.metadata = {"token_usage": settlement.to_message_metadata()}
            assistant_message.content = f"{assistant_message.content}\n\n{error_message}"
            await assistant_message.update()
        else:
            if reservation:
                await QUOTA_SERVICE.release(reservation, status="released")
            await cl.Message(content=error_message).send()
