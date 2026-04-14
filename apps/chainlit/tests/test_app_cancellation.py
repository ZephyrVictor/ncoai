from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as chainlit_app  # noqa: E402


class FakeMessage:
    def __init__(self, content: str, id: str | None = None, metadata=None, **_: object):
        self.id = id or "assistant-message-id"
        self.content = content
        self.metadata = metadata
        self.streaming = False
        self.is_error = False

    async def stream_token(self, token: str) -> None:
        self.content += token


class StreamCancelsAfterFirstToken:
    def __init__(self) -> None:
        self._seen_first = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._seen_first:
            self._seen_first = True
            return SimpleNamespace(
                id="provider-request-id",
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content="hello"))],
            )
        raise asyncio.CancelledError()


class CreateRaisesCancelledError:
    async def create(self, **_: object):
        raise asyncio.CancelledError()


class ReturnsCancellableStream:
    def __init__(self) -> None:
        self._stream = StreamCancelsAfterFirstToken()

    async def create(self, **_: object):
        return self._stream


def make_fake_client(chat_completions) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=chat_completions),
        close=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def patch_message_class(monkeypatch):
    monkeypatch.setattr(chainlit_app.cl, "Message", FakeMessage)


@pytest.fixture
def base_patches(monkeypatch):
    monkeypatch.setattr(
        chainlit_app,
        "current_managed_user",
        AsyncMock(return_value={"is_active": True, "is_deleted": False}),
    )
    monkeypatch.setattr(chainlit_app, "is_managed_user_active", Mock(return_value=True))
    monkeypatch.setattr(chainlit_app, "resolve_model_alias", Mock(return_value="test-model"))
    monkeypatch.setattr(chainlit_app, "current_user_identifier", Mock(return_value="user-1"))
    monkeypatch.setattr(chainlit_app, "active_assistant_message_id", Mock(return_value="assistant-1"))
    monkeypatch.setattr(
        chainlit_app,
        "context",
        SimpleNamespace(session=SimpleNamespace(thread_id="thread-1")),
    )
    monkeypatch.setattr(
        chainlit_app.cl,
        "chat_context",
        SimpleNamespace(to_openai=Mock(return_value=[{"role": "user", "content": "hello"}])),
    )
    monkeypatch.setattr(
        chainlit_app.cl,
        "user_session",
        SimpleNamespace(set=Mock()),
    )
    monkeypatch.setattr(chainlit_app, "emit_message_placeholder", AsyncMock())
    monkeypatch.setattr(chainlit_app, "emit_message_update", AsyncMock())
    monkeypatch.setattr(chainlit_app, "persist_message_snapshot", AsyncMock())
    monkeypatch.setattr(chainlit_app, "persist_request_metadata", AsyncMock())
    monkeypatch.setattr(chainlit_app, "close_async_resource", AsyncMock())


@pytest.mark.asyncio
async def test_cancelled_generation_settles_partial_usage(monkeypatch, base_patches):
    reservation = SimpleNamespace(
        request_id="reservation-1",
        allowed_completion_tokens=256,
    )
    settlement = SimpleNamespace(
        to_message_metadata=lambda: {
            "request_tokens": 42,
            "prompt_tokens": 12,
            "completion_tokens": 30,
        }
    )
    quota_service = SimpleNamespace(
        reserve=AsyncMock(return_value=reservation),
        settle=AsyncMock(return_value=settlement),
        release=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "QUOTA_SERVICE", quota_service)
    monkeypatch.setattr(
        chainlit_app,
        "build_client",
        Mock(return_value=make_fake_client(ReturnsCancellableStream())),
    )

    await chainlit_app.on_message(FakeMessage(content="user prompt", id="user-message-id"))

    quota_service.settle.assert_awaited_once_with(
        reservation=reservation,
        provider_request_id="provider-request-id",
        assistant_content="hello",
        usage=None,
    )
    quota_service.release.assert_not_called()
    chainlit_app.emit_message_update.assert_awaited_once()
    assistant_message = chainlit_app.emit_message_update.await_args.args[0]
    assert assistant_message.metadata["generation_status"] == "stopped"
    assert assistant_message.metadata["token_usage"]["request_tokens"] == 42
    chainlit_app.persist_request_metadata.assert_awaited_once_with(
        "provider-request-id",
        "test-model",
    )


@pytest.mark.asyncio
async def test_cancelled_generation_releases_unstarted_request(monkeypatch, base_patches):
    reservation = SimpleNamespace(
        request_id="reservation-2",
        allowed_completion_tokens=256,
    )
    quota_service = SimpleNamespace(
        reserve=AsyncMock(return_value=reservation),
        settle=AsyncMock(),
        release=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "QUOTA_SERVICE", quota_service)
    monkeypatch.setattr(
        chainlit_app,
        "build_client",
        Mock(return_value=make_fake_client(CreateRaisesCancelledError())),
    )

    await chainlit_app.on_message(FakeMessage(content="user prompt", id="user-message-id"))

    quota_service.settle.assert_not_called()
    quota_service.release.assert_awaited_once_with(reservation, status="cancelled")
    chainlit_app.emit_message_update.assert_awaited_once()
    assistant_message = chainlit_app.emit_message_update.await_args.args[0]
    assert assistant_message.metadata == {"generation_status": "stopped"}
    chainlit_app.persist_request_metadata.assert_awaited_once_with(
        None,
        "test-model",
    )
