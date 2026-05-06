from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
APP_DIR = ROOT_DIR / "apps" / "lastdance_proxy"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

MODULE_SPEC = importlib.util.spec_from_file_location(
    "ncoai_lastdance_proxy_app_for_tests",
    APP_DIR / "app.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
lastdance = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = lastdance
MODULE_SPEC.loader.exec_module(lastdance)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: str = "",
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self._lines = lines or []

    async def aread(self) -> bytes:
        return self._body.encode("utf-8")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamContext:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeClient:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.closed = False

    def stream(self, *args, **kwargs):
        return FakeStreamContext(self.outcome)

    async def aclose(self) -> None:
        self.closed = True


class FakeOpenedStream:
    def __init__(
        self,
        lines: list[str],
        *,
        upstream_name: str = "lastdance",
        response_model: str = "test-model",
    ) -> None:
        self.upstream_name = upstream_name
        self.response_model = response_model
        self.lines = lines
        self.closed = False

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def close(self) -> None:
        self.closed = True


def test_build_bigmodel_payload_uses_fixed_request_body() -> None:
    payload = lastdance.build_bigmodel_payload(
        [{"role": "user", "content": "hello"}]
    )

    assert payload == {
        "model": "glm-4.5-air",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled"},
        "stream": True,
        "max_tokens": 65536,
        "temperature": 1.0,
    }


def test_build_chatanywhere_payload_uses_gpt41nano_and_maps_max_completion_tokens() -> None:
    payload = lastdance.build_chatanywhere_payload(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.2,
            "max_completion_tokens": 256,
        }
    )

    assert payload == {
        "model": "gpt-4.1-nano-ca",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "temperature": 0.2,
        "max_tokens": 256,
    }


def test_extract_visible_content_ignores_reasoning_content() -> None:
    assert (
        lastdance.extract_visible_content_from_sse_line(
            'data: {"choices":[{"delta":{"reasoning_content":"分析"}}]}'
        )
        == []
    )
    assert lastdance.extract_visible_content_from_sse_line(
        'data: {"choices":[{"delta":{"content":"回答"}}]}'
    ) == ["回答"]


def test_acquire_request_slot_times_out_when_concurrency_is_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(lastdance, "REQUEST_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(lastdance, "QUEUE_TIMEOUT_SECONDS", 0.01)

    assert asyncio.run(lastdance.acquire_request_slot()) is False


def test_open_upstream_stream_retries_on_429(monkeypatch) -> None:
    outcomes = [
        FakeResponse(status_code=429, body="rate limited"),
        FakeResponse(status_code=200, lines=["data: [DONE]"]),
    ]
    created_clients: list[FakeClient] = []

    def fake_create_upstream_client_with_timeout(_timeout_seconds: float) -> FakeClient:
        client = FakeClient(outcomes.pop(0))
        created_clients.append(client)
        return client

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        lastdance,
        "create_upstream_client_with_timeout",
        fake_create_upstream_client_with_timeout,
    )
    monkeypatch.setattr(lastdance, "retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(lastdance.asyncio, "sleep", fake_sleep)

    opened_stream = asyncio.run(
        lastdance.open_upstream_stream_with_retries(
            upstream_name="lastdance",
            response_model="glm-4.5-air",
            url="https://example.com/v1/chat/completions",
            api_key="sk-test",
            payload={"messages": [{"role": "user", "content": "hello"}]},
            timeout_seconds=30.0,
        )
    )

    assert isinstance(opened_stream, lastdance.OpenedUpstreamStream)
    assert len(created_clients) == 2


def test_open_upstream_stream_does_not_retry_on_non_retryable_4xx(monkeypatch) -> None:
    attempts = 0

    def fake_create_upstream_client_with_timeout(_timeout_seconds: float) -> FakeClient:
        nonlocal attempts
        attempts += 1
        return FakeClient(FakeResponse(status_code=400, body="bad request"))

    monkeypatch.setattr(
        lastdance,
        "create_upstream_client_with_timeout",
        fake_create_upstream_client_with_timeout,
    )

    try:
        asyncio.run(
            lastdance.open_upstream_stream_with_retries(
                upstream_name="lastdance",
                response_model="glm-4.5-air",
                url="https://example.com/v1/chat/completions",
                api_key="sk-test",
                payload={"messages": [{"role": "user", "content": "hello"}]},
                timeout_seconds=30.0,
            )
        )
    except lastdance.UpstreamFailure as exc:
        assert exc.status_code == 400
        assert attempts == 1
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected UpstreamFailure for non-retryable 4xx")


def test_open_upstream_stream_with_failover_uses_chatanywhere_after_bigmodel_failure(
    monkeypatch,
) -> None:
    async def fake_open_upstream_stream_with_retries(
        *,
        upstream_name,
        response_model,
        url,
        api_key,
        payload,
        timeout_seconds,
        max_attempts=lastdance.MAX_ATTEMPTS,
    ):
        if upstream_name == "vllm":
            raise lastdance.UpstreamFailure(
                upstream_name="vllm",
                status_code=504,
                detail="timeout",
                transient=True,
            )
        if upstream_name == "lastdance":
            raise lastdance.UpstreamFailure(
                upstream_name="lastdance",
                status_code=504,
                detail="timeout",
                transient=True,
            )
        assert upstream_name == "chatanywhere"
        assert payload["model"] == "gpt-4.1-nano-ca"
        return FakeOpenedStream(
            ["data: [DONE]"],
            upstream_name="chatanywhere",
            response_model="gpt-4.1-nano-ca",
        )

    monkeypatch.setattr(
        lastdance,
        "open_upstream_stream_with_retries",
        fake_open_upstream_stream_with_retries,
    )

    opened_stream = asyncio.run(
        lastdance.open_upstream_stream_with_failover(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
    )

    assert opened_stream.upstream_name == "chatanywhere"


def test_open_upstream_stream_with_failover_prefers_local_vllm_when_available(
    monkeypatch,
) -> None:
    async def fake_open_upstream_stream_with_retries(
        *,
        upstream_name,
        response_model,
        url,
        api_key,
        payload,
        timeout_seconds,
        max_attempts=lastdance.MAX_ATTEMPTS,
    ):
        assert upstream_name == "vllm"
        assert payload["model"] == "qwen35b"
        return FakeOpenedStream(
            ["data: [DONE]"],
            upstream_name="vllm",
            response_model="qwen35b",
        )

    monkeypatch.setattr(lastdance, "lastdance_enabled", lambda: False)
    monkeypatch.setattr(
        lastdance,
        "open_upstream_stream_with_retries",
        fake_open_upstream_stream_with_retries,
    )

    opened_stream = asyncio.run(
        lastdance.open_upstream_stream_with_failover(
            {
                "model": "qwen35b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
    )

    assert opened_stream.upstream_name == "vllm"


def test_chat_completions_returns_503_when_queue_is_full(monkeypatch) -> None:
    async def fake_acquire_request_slot() -> bool:
        return False

    monkeypatch.setattr(lastdance, "acquire_request_slot", fake_acquire_request_slot)

    with TestClient(lastdance.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 503


def test_streaming_endpoint_forwards_only_visible_content(monkeypatch) -> None:
    async def fake_acquire_request_slot() -> bool:
        return True

    async def fake_open_upstream_stream_with_failover(payload):
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        return FakeOpenedStream(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"分析"}}]}',
                'data: {"choices":[{"delta":{"content":"你"}}]}',
                'data: {"choices":[{"delta":{"content":"好"}}]}',
                "data: [DONE]",
            ],
            response_model="qwen35b",
        )

    monkeypatch.setattr(lastdance, "acquire_request_slot", fake_acquire_request_slot)
    monkeypatch.setattr(
        lastdance,
        "open_upstream_stream_with_failover",
        fake_open_upstream_stream_with_failover,
    )

    with TestClient(lastdance.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "分析" not in response.text
    assert "你" in response.text
    assert "好" in response.text
    assert "[DONE]" in response.text
