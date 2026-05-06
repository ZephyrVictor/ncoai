from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import os
import random
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ncoai_shared.monitoring import AppMetrics

LOGGER = logging.getLogger("ncoai.lastdance")
APP_METRICS = AppMetrics("lastdance")

BIGMODEL_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
CHATANYWHERE_URL = "https://api.chatanywhere.tech/v1/chat/completions"
TARGET_MODEL = "glm-4.5-air"
CHATANYWHERE_MODEL = "gpt-4.1-nano-ca"
BIGMODEL_API_KEY = "555d6bac07694ed7aff0d5c83ad768f5.YdFGi0jnPGys1KZS"
CHATANYWHERE_API_KEY = "sk-AC11MFAsFq7eIw78060OEZFkd5DJ3nYRn4TV5g9Pkj01jE3I"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 12234
CONCURRENCY_LIMIT = 10
QUEUE_TIMEOUT_SECONDS = 2.0
MAX_ATTEMPTS = 3
RETRY_BASE_DELAYS = (0.5, 1.0)

REQUEST_SEMAPHORE = asyncio.Semaphore(CONCURRENCY_LIMIT)


@dataclass
class OpenedUpstreamStream:
    upstream_name: str
    response_model: str
    client: httpx.AsyncClient
    stream_context: Any
    response: httpx.Response

    async def aiter_lines(self) -> AsyncIterator[str]:
        async for line in self.response.aiter_lines():
            yield line

    async def close(self) -> None:
        await self.stream_context.__aexit__(None, None, None)
        await self.client.aclose()


@dataclass
class UpstreamFailure(Exception):
    upstream_name: str
    status_code: int
    detail: str
    transient: bool

    def __str__(self) -> str:
        return (
            f"{self.upstream_name} status={self.status_code} "
            f"transient={self.transient} detail={self.detail}"
        )


def lastdance_enabled() -> bool:
    return str(os.getenv("thisisthelastdance", "0")).strip() == "1"


def upstream_timeout_seconds() -> float:
    raw = str(os.getenv("CHAINLIT_OPENAI_TIMEOUT", "300")).strip() or "300"
    try:
        return float(raw)
    except ValueError:
        return 300.0


def local_vllm_origin() -> str:
    host = str(os.getenv("VLLM_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port = str(os.getenv("VLLM_PORT", "12233")).strip() or "12233"
    return f"http://{host}:{port}"


def local_vllm_chat_url() -> str:
    return f"{local_vllm_origin()}/v1/chat/completions"


def served_model_name() -> str:
    return str(os.getenv("SERVED_MODEL_NAME", "qwen35b")).strip() or "qwen35b"


def local_vllm_timeout_seconds() -> float:
    raw = str(os.getenv("VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS", "5")).strip() or "5"
    try:
        return float(raw)
    except ValueError:
        return 5.0


def build_bigmodel_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": TARGET_MODEL,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "stream": True,
        "max_tokens": 65536,
        "temperature": 1.0,
    }


def build_chatanywhere_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    messages = request_payload.get("messages")
    payload: dict[str, Any] = {
        "model": CHATANYWHERE_MODEL,
        "messages": messages,
        "stream": True,
    }

    passthrough_fields = (
        "temperature",
        "top_p",
        "n",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "user",
        "stream_options",
        "logprobs",
        "top_logprobs",
        "response_format",
        "tools",
        "tool_choice",
    )
    for field_name in passthrough_fields:
        if field_name in request_payload and request_payload[field_name] is not None:
            payload[field_name] = request_payload[field_name]

    if request_payload.get("max_tokens") is not None:
        payload["max_tokens"] = request_payload["max_tokens"]
    elif request_payload.get("max_completion_tokens") is not None:
        payload["max_tokens"] = request_payload["max_completion_tokens"]

    return payload


def extract_visible_content_from_sse_line(line: str) -> list[str]:
    if not line.startswith("data:"):
        return []

    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return []

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []

    visible: list[str] = []
    for choice in data.get("choices") or []:
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            visible.append(content)
    return visible


def should_retry_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def should_retry_exception(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    )


def retry_backoff_seconds(attempt: int) -> float:
    index = min(max(attempt - 1, 0), len(RETRY_BASE_DELAYS) - 1)
    return RETRY_BASE_DELAYS[index] + random.uniform(0.0, 0.15)


def create_upstream_client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(upstream_timeout_seconds())
    return httpx.AsyncClient(timeout=timeout)


def create_upstream_client_with_timeout(timeout_seconds: float) -> httpx.AsyncClient:
    timeout = httpx.Timeout(timeout_seconds)
    return httpx.AsyncClient(timeout=timeout)


def request_headers(*, api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


async def acquire_request_slot() -> bool:
    try:
        await asyncio.wait_for(REQUEST_SEMAPHORE.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
        return True
    except asyncio.TimeoutError:
        return False


def release_request_slot() -> None:
    REQUEST_SEMAPHORE.release()


def build_openai_chunk(
    *,
    completion_id: str,
    created: int,
    model_name: str,
    content: str | None = None,
    finish_reason: str | None = None,
) -> bytes:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def build_openai_completion(
    *,
    completion_id: str,
    created: int,
    model_name: str,
    content: str,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
    }


async def open_upstream_stream_with_retries(
    *,
    upstream_name: str,
    response_model: str,
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    max_attempts: int = MAX_ATTEMPTS,
) -> OpenedUpstreamStream:
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        client = create_upstream_client_with_timeout(timeout_seconds)
        stream_context = client.stream(
            "POST",
            url,
            headers=request_headers(api_key=api_key),
            json=payload,
        )
        try:
            response = await stream_context.__aenter__()
        except Exception as exc:
            await client.aclose()
            last_error = str(exc)
            APP_METRICS.record_upstream_request(upstream_name, "exception")
            if should_retry_exception(exc) and attempt < max_attempts:
                delay = retry_backoff_seconds(attempt)
                LOGGER.warning(
                    "Retrying upstream=%s attempt=%s reason=%s backoff_seconds=%.2f",
                    upstream_name,
                    attempt + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            LOGGER.error(
                "Upstream=%s request failed after %s attempts: %s",
                upstream_name,
                attempt,
                exc,
            )
            detail = f"{upstream_name} upstream request failed"
            if isinstance(exc, httpx.TimeoutException):
                detail = f"{upstream_name} upstream timed out"
            raise UpstreamFailure(
                upstream_name=upstream_name,
                status_code=504 if isinstance(exc, httpx.TimeoutException) else 502,
                detail=detail,
                transient=True,
            ) from exc

        if 200 <= response.status_code < 300:
            APP_METRICS.record_upstream_request(upstream_name, "success")
            return OpenedUpstreamStream(
                upstream_name=upstream_name,
                response_model=response_model,
                client=client,
                stream_context=stream_context,
                response=response,
            )

        detail = (await response.aread()).decode("utf-8", errors="ignore").strip()
        await stream_context.__aexit__(None, None, None)
        await client.aclose()

        last_error = f"HTTP {response.status_code}: {detail or 'upstream error'}"
        APP_METRICS.record_upstream_request(
            upstream_name,
            f"http_{response.status_code}",
        )
        if should_retry_status(response.status_code) and attempt < max_attempts:
            delay = retry_backoff_seconds(attempt)
            LOGGER.warning(
                "Retrying upstream=%s attempt=%s reason=%s backoff_seconds=%.2f",
                upstream_name,
                attempt + 1,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        LOGGER.error(
            "Upstream=%s request failed after %s attempts: %s",
            upstream_name,
            attempt,
            last_error,
        )
        raise UpstreamFailure(
            upstream_name=upstream_name,
            status_code=response.status_code,
            detail=detail or f"{upstream_name} upstream request failed",
            transient=should_retry_status(response.status_code),
        )

    LOGGER.error(
        "Upstream=%s request exhausted retries: %s",
        upstream_name,
        last_error or "unknown",
    )
    raise UpstreamFailure(
        upstream_name=upstream_name,
        status_code=502,
        detail=f"{upstream_name} upstream request failed",
        transient=True,
    )


def should_failover_to_chatanywhere(failure: UpstreamFailure) -> bool:
    return failure.transient


async def open_upstream_stream_with_failover(
    request_payload: dict[str, Any],
) -> OpenedUpstreamStream:
    if not lastdance_enabled():
        try:
            return await open_upstream_stream_with_retries(
                upstream_name="vllm",
                response_model=str(request_payload.get("model") or served_model_name()),
                url=local_vllm_chat_url(),
                api_key="dummy",
                payload=dict(request_payload),
                timeout_seconds=local_vllm_timeout_seconds(),
                max_attempts=1,
            )
        except UpstreamFailure as exc:
            LOGGER.warning(
                "Local vLLM unavailable status=%s detail=%s, switching to external fallback chain",
                exc.status_code,
                exc.detail,
            )
            APP_METRICS.record_upstream_request("vllm", "failover_to_lastdance")

    bigmodel_payload = build_bigmodel_payload(request_payload["messages"])
    try:
        return await open_upstream_stream_with_retries(
            upstream_name="lastdance",
            response_model=TARGET_MODEL,
            url=BIGMODEL_URL,
            api_key=BIGMODEL_API_KEY,
            payload=bigmodel_payload,
            timeout_seconds=upstream_timeout_seconds(),
        )
    except UpstreamFailure as exc:
        if not should_failover_to_chatanywhere(exc):
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        LOGGER.warning(
            "BigModel fallback triggered upstream=%s status=%s detail=%s, switching to chatanywhere",
            exc.upstream_name,
            exc.status_code,
            exc.detail,
        )
        APP_METRICS.record_upstream_request("lastdance", "failover_to_chatanywhere")

    try:
        return await open_upstream_stream_with_retries(
            upstream_name="chatanywhere",
            response_model=CHATANYWHERE_MODEL,
            url=CHATANYWHERE_URL,
            api_key=CHATANYWHERE_API_KEY,
            payload=build_chatanywhere_payload(request_payload),
            timeout_seconds=upstream_timeout_seconds(),
        )
    except UpstreamFailure as exc:
        LOGGER.error(
            "ChatAnywhere fallback failed after BigModel failover status=%s detail=%s",
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def stream_visible_content(opened_stream: OpenedUpstreamStream) -> AsyncIterator[bytes]:
    completion_id = f"lastdance-{uuid.uuid4().hex}"
    created = int(time.time())
    try:
        async for line in opened_stream.aiter_lines():
            for content in extract_visible_content_from_sse_line(line):
                yield build_openai_chunk(
                    completion_id=completion_id,
                    created=created,
                    model_name=opened_stream.response_model,
                    content=content,
                )
        yield build_openai_chunk(
            completion_id=completion_id,
            created=created,
            model_name=opened_stream.response_model,
            finish_reason="stop",
        )
        yield b"data: [DONE]\n\n"
    except Exception as exc:
        LOGGER.error("Streaming forward failed upstream=%s error=%s", opened_stream.upstream_name, exc)
        APP_METRICS.record_upstream_request(opened_stream.upstream_name, "stream_error")
        raise
    finally:
        await opened_stream.close()
        release_request_slot()


async def collect_visible_content(opened_stream: OpenedUpstreamStream) -> str:
    visible_parts: list[str] = []
    try:
        async for line in opened_stream.aiter_lines():
            visible_parts.extend(extract_visible_content_from_sse_line(line))
        return "".join(visible_parts)
    except Exception as exc:
        LOGGER.error(
            "Non-stream forward failed upstream=%s error=%s",
            opened_stream.upstream_name,
            exc,
        )
        APP_METRICS.record_upstream_request(opened_stream.upstream_name, "stream_error")
        raise HTTPException(status_code=502, detail="Lastdance upstream stream failed") from exc
    finally:
        await opened_stream.close()
        release_request_slot()


@asynccontextmanager
async def lifespan(_: FastAPI):
    LOGGER.info(
        "Starting Lastdance proxy enabled=%s listen=%s:%s local_vllm_origin=%s target_model=%s fallback_model=%s concurrency_limit=%s",
        lastdance_enabled(),
        PROXY_HOST,
        PROXY_PORT,
        local_vllm_origin(),
        TARGET_MODEL,
        CHATANYWHERE_MODEL,
        CONCURRENCY_LIMIT,
    )
    yield


app = FastAPI(lifespan=lifespan)
app.middleware("http")(APP_METRICS.instrument_http)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "lastdance-proxy",
            "enabled": lastdance_enabled(),
            "local_vllm_origin": local_vllm_origin(),
            "served_model_name": served_model_name(),
            "target_model": TARGET_MODEL,
            "fallback_model": CHATANYWHERE_MODEL,
            "concurrency_limit": CONCURRENCY_LIMIT,
        }
    )


@app.get("/metrics")
async def metrics() -> Response:
    return APP_METRICS.render_metrics()


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": served_model_name(),
                    "object": "model",
                    "owned_by": "vllm-router",
                },
                {
                    "id": TARGET_MODEL,
                    "object": "model",
                    "owned_by": "lastdance",
                },
                {
                    "id": CHATANYWHERE_MODEL,
                    "object": "model",
                    "owned_by": "lastdance-fallback",
                }
            ],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    payload = await request.json()
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")

    stream_requested = bool(payload.get("stream"))
    LOGGER.info(
        "Lastdance request received stream=%s messages=%s",
        stream_requested,
        len(messages),
    )

    acquired = await acquire_request_slot()
    if not acquired:
        LOGGER.warning(
            "Lastdance concurrency limit reached limit=%s queue_timeout_seconds=%.1f",
            CONCURRENCY_LIMIT,
            QUEUE_TIMEOUT_SECONDS,
        )
        APP_METRICS.record_upstream_request("lastdance", "queue_timeout")
        raise HTTPException(status_code=503, detail="Lastdance proxy is busy")

    try:
        opened_stream = await open_upstream_stream_with_failover(payload)
    except Exception:
        release_request_slot()
        raise

    if stream_requested:
        return StreamingResponse(
            stream_visible_content(opened_stream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    completion_id = f"lastdance-{uuid.uuid4().hex}"
    created = int(time.time())
    content = await collect_visible_content(opened_stream)
    return JSONResponse(
        build_openai_completion(
            completion_id=completion_id,
            created=created,
            model_name=opened_stream.response_model,
            content=content,
        )
    )
