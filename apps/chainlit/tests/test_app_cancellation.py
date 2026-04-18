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


class SuccessfulStream:
    def __init__(self, *tokens: str) -> None:
        self._parts = [
            SimpleNamespace(
                id="provider-request-id",
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content=token))],
            )
            for token in tokens
        ]
        self._parts.append(
            SimpleNamespace(
                id="provider-request-id",
                usage=SimpleNamespace(
                    prompt_tokens=12,
                    completion_tokens=30,
                    total_tokens=42,
                ),
                choices=[],
            )
        )
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._parts):
            raise StopAsyncIteration
        part = self._parts[self._index]
        self._index += 1
        return part


class ReturnsSuccessfulStream:
    def __init__(self, *tokens: str) -> None:
        self._stream = SuccessfulStream(*tokens)

    async def create(self, **_: object):
        return self._stream


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
    monkeypatch.setattr(
        chainlit_app,
        "acquire_litellm_request_slot",
        AsyncMock(return_value=(True, 0.0)),
    )
    monkeypatch.setattr(
        chainlit_app,
        "GLOBAL_ADMISSION_CONTROLLER",
        SimpleNamespace(
            is_saturated=AsyncMock(return_value=False),
            acquire=AsyncMock(return_value=(SimpleNamespace(holder_id="lease-1"), 0.0)),
            release=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        chainlit_app,
        "GENERATION_REGISTRY",
        SimpleNamespace(
            start=AsyncMock(),
            append_token=AsyncMock(),
            set_status=AsyncMock(),
            set_content=AsyncMock(),
            snapshot_due=AsyncMock(return_value=False),
            mark_snapshot=AsyncMock(),
            get=AsyncMock(return_value=None),
            remove=AsyncMock(),
        ),
    )


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


@pytest.mark.asyncio
async def test_admission_is_acquired_before_quota_reserve(monkeypatch, base_patches):
    call_order: list[str] = []
    reservation = SimpleNamespace(
        request_id="reservation-3",
        allowed_completion_tokens=256,
        thread_summary=SimpleNamespace(used_tokens=0),
        user_summary=SimpleNamespace(used_tokens=0),
    )
    settlement = SimpleNamespace(
        to_message_metadata=lambda: {
            "request_tokens": 42,
            "prompt_tokens": 12,
            "completion_tokens": 30,
        }
    )

    async def reserve(**_: object):
        call_order.append("reserve")
        return reservation

    async def acquire():
        call_order.append("admission")
        return SimpleNamespace(holder_id="lease-2"), 0.0

    quota_service = SimpleNamespace(
        reserve=AsyncMock(side_effect=reserve),
        settle=AsyncMock(return_value=settlement),
        release=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "QUOTA_SERVICE", quota_service)
    monkeypatch.setattr(
        chainlit_app,
        "GLOBAL_ADMISSION_CONTROLLER",
        SimpleNamespace(
            is_saturated=AsyncMock(return_value=False),
            acquire=AsyncMock(side_effect=acquire),
            release=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        chainlit_app,
        "build_client",
        Mock(return_value=make_fake_client(ReturnsSuccessfulStream("hello"))),
    )

    await chainlit_app.on_message(FakeMessage(content="user prompt", id="user-message-id"))

    assert call_order[:2] == ["admission", "reserve"]


@pytest.mark.asyncio
async def test_admission_timeout_does_not_create_quota_reservation(
    monkeypatch, base_patches
):
    quota_service = SimpleNamespace(
        reserve=AsyncMock(),
        settle=AsyncMock(),
        release=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "QUOTA_SERVICE", quota_service)
    monkeypatch.setattr(
        chainlit_app,
        "GLOBAL_ADMISSION_CONTROLLER",
        SimpleNamespace(
            is_saturated=AsyncMock(return_value=True),
            acquire=AsyncMock(return_value=(None, 8.0)),
            release=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        chainlit_app,
        "build_client",
        Mock(return_value=make_fake_client(ReturnsSuccessfulStream("hello"))),
    )

    await chainlit_app.on_message(FakeMessage(content="user prompt", id="user-message-id"))

    quota_service.reserve.assert_not_called()
    assert chainlit_app.emit_message_update.await_count == 2
    assistant_message = chainlit_app.emit_message_update.await_args.args[0]
    assert "排队等待超时" in assistant_message.content


@pytest.mark.asyncio
async def test_streaming_tokens_skip_periodic_snapshot_until_finalize(
    monkeypatch, base_patches
):
    reservation = SimpleNamespace(
        request_id="reservation-4",
        allowed_completion_tokens=256,
        thread_summary=SimpleNamespace(used_tokens=0),
        user_summary=SimpleNamespace(used_tokens=0),
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
    generation_registry = SimpleNamespace(
        start=AsyncMock(),
        append_token=AsyncMock(),
        set_status=AsyncMock(),
        set_content=AsyncMock(),
        snapshot_due=AsyncMock(side_effect=[False, False]),
        mark_snapshot=AsyncMock(),
        get=AsyncMock(return_value=None),
        remove=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "QUOTA_SERVICE", quota_service)
    monkeypatch.setattr(chainlit_app, "GENERATION_REGISTRY", generation_registry)
    monkeypatch.setattr(
        chainlit_app,
        "build_client",
        Mock(return_value=make_fake_client(ReturnsSuccessfulStream("he", "llo"))),
    )

    await chainlit_app.on_message(FakeMessage(content="user prompt", id="user-message-id"))

    assert generation_registry.snapshot_due.await_count == 2
    chainlit_app.persist_message_snapshot.assert_awaited_once()
