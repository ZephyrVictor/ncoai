from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import socketio

from QPS_test.common import (
    RequestRecord,
    RunConfig,
    ThreadState,
    UserState,
    build_cookie_header,
    build_socketio_path,
    serialize_datetime,
    utcnow,
)
from QPS_test.prompting import PromptFactory, get_traffic_profile
from QPS_test.reporting import RequestRecorder
from QPS_test.runtime import LogCapMonitor, ResourceSafetyMonitor


STOP_MIN_COMPLETION_TOKENS = 48
STOP_MAX_COMPLETION_TOKENS = 160


@dataclass(slots=True)
class InteractionOutcome:
    record: RequestRecord
    resolved_thread_id: str | None
    session_used_tokens: int | None
    user_used_tokens: int | None


@dataclass(slots=True)
class _InteractionState:
    session_id: str
    request_sent_at: datetime | None = None
    first_token_at: datetime | None = None
    task_end_at: datetime | None = None
    stop_sent_at: datetime | None = None
    resolved_thread_id: str | None = None
    token_usage: dict[str, Any] = field(default_factory=dict)
    quota_status: dict[str, Any] = field(default_factory=dict)
    assistant_status: str | None = None
    failure_type: str | None = None
    http_or_ws_error: str | None = None
    request_active: bool = False
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    streamed_text_parts: list[str] = field(default_factory=list)
    streamed_completion_tokens_estimated: int = 0
    stop_trigger_tokens_estimated: int | None = None
    stopped_by_disturbance: bool = False
    natural_completion: bool = False


class ChainlitProtocolClient:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self._profile = get_traffic_profile(config.traffic_profile)
        self._prompt_factory = PromptFactory(
            system_prompt=config.system_prompt,
            encoding_name=config.tiktoken_encoding,
            input_token_target=config.input_token_target,
            prompt_safety_tokens=config.prompt_safety_tokens,
            max_completion_tokens=config.runtime_completion_tokens,
        )
        split = urlsplit(config.chat_base_url.rstrip("/"))
        self._origin = f"{split.scheme}://{split.netloc}"
        self._socketio_path = build_socketio_path(config.chat_base_url)
        self._login_locks: dict[str, asyncio.Lock] = {}

    @property
    def prompt_factory(self) -> PromptFactory:
        return self._prompt_factory

    @property
    def traffic_profile(self):
        return self._profile

    async def ensure_logged_in(self, user: UserState) -> None:
        if user.auth_cookies:
            return
        lock = self._login_locks.setdefault(user.credential.identifier, asyncio.Lock())
        async with lock:
            if user.auth_cookies:
                return
            async with httpx.AsyncClient(
                base_url=self.config.chat_base_url,
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                response = await client.post(
                    "/login",
                    data={
                        "username": user.credential.identifier,
                        "password": user.credential.password,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success"):
                    raise RuntimeError(
                        f"managed user login failed for {user.credential.identifier}"
                    )
                user.auth_cookies = dict(client.cookies.items())

    async def prelogin_users(
        self,
        users: Iterable[UserState],
        *,
        concurrency: int,
    ) -> None:
        semaphore = asyncio.Semaphore(max(concurrency, 1))

        async def login_one(user: UserState) -> None:
            async with semaphore:
                await self.ensure_logged_in(user)

        await asyncio.gather(*(login_one(user) for user in users))

    def _build_message_payload(
        self,
        *,
        user: UserState,
        request_seq: int,
        behavior: str,
        prompt: str,
        conversation_turn_index: int,
        topic_name: str,
    ) -> tuple[dict[str, Any], str]:
        assistant_message_id = str(uuid.uuid4())
        message = {
            "threadId": "",
            "id": str(uuid.uuid4()),
            "name": user.credential.identifier,
            "type": "user_message",
            "output": prompt,
            "createdAt": serialize_datetime(utcnow()),
            "metadata": {
                "location": f"{self.config.chat_base_url}/",
                "behavior_type": behavior,
                "request_seq": request_seq,
                "conversation_turn_index": conversation_turn_index,
                "topic_name": topic_name,
                "mode": self.config.mode,
            },
        }
        return (
            {
                "message": message,
                "fileReferences": [],
                "assistantMessageId": assistant_message_id,
            },
            assistant_message_id,
        )

    def _apply_assistant_event(
        self,
        state: _InteractionState,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict):
            return
        payload_type = str(payload.get("type") or "")
        if payload_type and payload_type != "assistant_message":
            return
        if payload.get("threadId") and not state.resolved_thread_id:
            state.resolved_thread_id = str(payload["threadId"])
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        token_usage = metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            state.token_usage = dict(token_usage)
            if not state.resolved_thread_id and payload.get("threadId"):
                state.resolved_thread_id = str(payload.get("threadId"))
        quota_status = metadata.get("quota_status") or {}
        if isinstance(quota_status, dict):
            state.quota_status = dict(quota_status)
        generation_status = metadata.get("generation_status")
        if generation_status:
            state.assistant_status = str(generation_status)
        if payload.get("isError"):
            state.failure_type = "assistant_error"

    def _extract_stream_text(self, payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        if isinstance(payload, dict):
            for key in ("token", "content", "output", "text"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value
            if isinstance(payload.get("delta"), dict):
                value = payload["delta"].get("content")
                if isinstance(value, str):
                    return value
        return ""

    def _reported_completion_tokens(self, state: _InteractionState) -> int | None:
        completion_tokens = _int_or_none(state.token_usage.get("completion_tokens"))
        if completion_tokens is not None:
            return completion_tokens
        total_tokens = _int_or_none(state.token_usage.get("request_tokens")) or _int_or_none(
            state.token_usage.get("total_tokens")
        )
        prompt_tokens = _int_or_none(state.token_usage.get("prompt_tokens"))
        if total_tokens is None or prompt_tokens is None:
            return None
        return max(total_tokens - prompt_tokens, 0)

    def random_stop_threshold(self, rng: random.Random) -> int:
        return rng.randint(STOP_MIN_COMPLETION_TOKENS, STOP_MAX_COMPLETION_TOKENS)

    async def run_request(
        self,
        *,
        user: UserState,
        thread: ThreadState,
        request_seq: int,
        behavior: str,
        conversation_turn_index: int,
        session_rollover_reason: str | None,
        stop_trigger_tokens_estimated: int | None,
    ) -> InteractionOutcome:
        await self.ensure_logged_in(user)
        session_id = str(uuid.uuid4())
        prompt = self._prompt_factory.build_prompt(
            user_id=user.credential.identifier,
            request_seq=request_seq,
            conversation_turn_index=conversation_turn_index,
            topic_name=thread.topic_name,
        )
        input_tokens_estimated = self._prompt_factory.estimate_text_tokens(prompt)
        state = _InteractionState(
            session_id=session_id,
            stop_trigger_tokens_estimated=stop_trigger_tokens_estimated,
        )

        async with httpx.AsyncClient(
            base_url=self.config.chat_base_url,
            follow_redirects=True,
            timeout=30.0,
            cookies=user.auth_cookies,
        ) as client:
            try:
                sticky_response = await client.post(
                    "/set-session-cookie",
                    json={"session_id": session_id},
                )
                sticky_response.raise_for_status()
                settings_response = await client.get("/project/settings")
                settings_response.raise_for_status()
            except Exception as exc:
                started_at = utcnow()
                record = RequestRecord(
                    run_id=self.config.run_id,
                    user_id=user.credential.identifier,
                    thread_id=thread.thread_id,
                    session_id=session_id,
                    conversation_id=thread.thread_id or thread.local_name,
                    behavior_type=behavior,
                    request_seq=request_seq,
                    conversation_turn_index=conversation_turn_index,
                    session_rollover_index=thread.rollover_index,
                    request_sent_at=serialize_datetime(started_at) or "",
                    first_token_at=None,
                    task_end_at=serialize_datetime(utcnow()),
                    ttft_ms=None,
                    wall_time_ms=0.0,
                    generation_duration_ms=None,
                    output_tokens_per_second=None,
                    stop_sent_at=None,
                    success=False,
                    failure_type="http_error",
                    http_or_ws_error=str(exc),
                    input_tokens_estimated=input_tokens_estimated,
                    prompt_tokens=None,
                    completion_tokens=None,
                    completion_tokens_reported_or_estimated=None,
                    total_tokens=None,
                    stopped_by_disturbance=False,
                    stop_trigger_tokens_estimated=stop_trigger_tokens_estimated,
                    natural_completion=False,
                    session_rollover_reason=session_rollover_reason,
                    quota_blocked_scope=None,
                    quota_blocked_reason=None,
                )
                return InteractionOutcome(
                    record=record,
                    resolved_thread_id=thread.thread_id,
                    session_used_tokens=None,
                    user_used_tokens=None,
                )

            sio = socketio.AsyncClient(
                reconnection=False,
                logger=False,
                engineio_logger=False,
            )

            @sio.event
            async def connect() -> None:
                await sio.emit("connection_successful")

            @sio.on("first_interaction")
            async def on_first_interaction(data: Any) -> None:
                if isinstance(data, dict) and data.get("thread_id"):
                    state.resolved_thread_id = str(data["thread_id"])

            @sio.on("stream_token")
            async def on_stream_token(data: Any) -> None:
                if not state.request_active:
                    return
                if state.first_token_at is None:
                    state.first_token_at = utcnow()
                fragment = self._extract_stream_text(data)
                if fragment:
                    state.streamed_text_parts.append(fragment)
                    state.streamed_completion_tokens_estimated += (
                        self._prompt_factory.estimate_fragment_tokens(fragment)
                    )
                if (
                    behavior == "stop"
                    and state.stop_sent_at is None
                    and state.stop_trigger_tokens_estimated is not None
                    and state.streamed_completion_tokens_estimated
                    >= state.stop_trigger_tokens_estimated
                ):
                    state.stop_sent_at = utcnow()
                    state.stopped_by_disturbance = True
                    await sio.emit("stop")

            @sio.on("new_message")
            async def on_new_message(data: Any) -> None:
                self._apply_assistant_event(state, data)

            @sio.on("update_message")
            async def on_update_message(data: Any) -> None:
                self._apply_assistant_event(state, data)

            @sio.on("task_end")
            async def on_task_end(*_args: Any) -> None:
                if state.request_active:
                    state.task_end_at = utcnow()
                    state.natural_completion = state.stop_sent_at is None
                    state.done_event.set()

            @sio.on("connect_error")
            async def on_connect_error(data: Any) -> None:
                state.failure_type = "connect_error"
                state.http_or_ws_error = str(data)
                state.done_event.set()

            cookie_header = build_cookie_header(dict(client.cookies.items()))
            auth_payload = {
                "clientType": "webapp",
                "sessionId": session_id,
                "threadId": thread.thread_id or "",
                "userEnv": "{}",
                "chatProfile": "",
            }
            message_payload, _assistant_message_id = self._build_message_payload(
                user=user,
                request_seq=request_seq,
                behavior=behavior,
                prompt=prompt,
                conversation_turn_index=conversation_turn_index,
                topic_name=thread.topic_name,
            )

            started_monotonic = time.perf_counter()
            request_sent_at = utcnow()
            try:
                await sio.connect(
                    self._origin,
                    socketio_path=self._socketio_path,
                    headers={"Cookie": cookie_header} if cookie_header else None,
                    auth=auth_payload,
                    wait_timeout=15,
                )
                await asyncio.sleep(0.05)
                state.request_active = True
                state.request_sent_at = request_sent_at
                await sio.emit("client_message", message_payload)
                await asyncio.wait_for(
                    state.done_event.wait(),
                    timeout=self.config.request_timeout_seconds,
                )
            except asyncio.TimeoutError:
                state.failure_type = state.failure_type or "timeout"
                state.http_or_ws_error = (
                    state.http_or_ws_error
                    or f"request exceeded {self.config.request_timeout_seconds}s"
                )
                state.task_end_at = utcnow()
            except Exception as exc:
                state.failure_type = state.failure_type or "ws_error"
                state.http_or_ws_error = state.http_or_ws_error or str(exc)
                state.task_end_at = utcnow()
            finally:
                state.request_active = False
                if sio.connected:
                    await sio.disconnect()

        wall_time_ms = (time.perf_counter() - started_monotonic) * 1000.0
        ttft_ms = None
        if state.first_token_at is not None:
            ttft_ms = (
                state.first_token_at - request_sent_at
            ).total_seconds() * 1000.0
        generation_duration_ms = None
        if state.first_token_at is not None and state.task_end_at is not None:
            generation_duration_ms = max(
                (state.task_end_at - state.first_token_at).total_seconds() * 1000.0,
                0.0,
            )

        prompt_tokens = _int_or_none(state.token_usage.get("prompt_tokens"))
        completion_tokens = self._reported_completion_tokens(state)
        estimated_completion_tokens = None
        if state.streamed_text_parts:
            estimated_completion_tokens = self._prompt_factory.estimate_text_tokens(
                "".join(state.streamed_text_parts)
            )
        completion_tokens_reported_or_estimated = (
            completion_tokens
            if completion_tokens is not None
            else estimated_completion_tokens
        )
        output_tokens_per_second = None
        if (
            generation_duration_ms is not None
            and generation_duration_ms > 0
            and completion_tokens_reported_or_estimated is not None
        ):
            output_tokens_per_second = completion_tokens_reported_or_estimated / (
                generation_duration_ms / 1000.0
            )

        quota_scope = str(state.quota_status.get("blocked_scope") or "") or None
        quota_reason = str(state.quota_status.get("reason") or "") or None
        total_tokens = _int_or_none(state.token_usage.get("request_tokens")) or _int_or_none(
            state.token_usage.get("total_tokens")
        )
        if total_tokens is None and completion_tokens_reported_or_estimated is not None:
            total_tokens = input_tokens_estimated + completion_tokens_reported_or_estimated

        success = True
        failure_type = state.failure_type
        if quota_scope:
            success = False
            failure_type = failure_type or "quota_blocked"
        elif failure_type:
            success = False
        else:
            success = state.task_end_at is not None

        record = RequestRecord(
            run_id=self.config.run_id,
            user_id=user.credential.identifier,
            thread_id=state.resolved_thread_id or thread.thread_id,
            session_id=session_id,
            conversation_id=state.resolved_thread_id or thread.thread_id or thread.local_name,
            behavior_type=behavior,
            request_seq=request_seq,
            conversation_turn_index=conversation_turn_index,
            session_rollover_index=thread.rollover_index,
            request_sent_at=serialize_datetime(request_sent_at) or "",
            first_token_at=serialize_datetime(state.first_token_at),
            task_end_at=serialize_datetime(state.task_end_at),
            ttft_ms=ttft_ms,
            wall_time_ms=wall_time_ms,
            generation_duration_ms=generation_duration_ms,
            output_tokens_per_second=output_tokens_per_second,
            stop_sent_at=serialize_datetime(state.stop_sent_at),
            success=success,
            failure_type=failure_type,
            http_or_ws_error=state.http_or_ws_error,
            input_tokens_estimated=input_tokens_estimated,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            completion_tokens_reported_or_estimated=completion_tokens_reported_or_estimated,
            total_tokens=total_tokens,
            stopped_by_disturbance=state.stopped_by_disturbance,
            stop_trigger_tokens_estimated=state.stop_trigger_tokens_estimated,
            natural_completion=state.natural_completion,
            session_rollover_reason=session_rollover_reason,
            quota_blocked_scope=quota_scope,
            quota_blocked_reason=quota_reason,
        )
        return InteractionOutcome(
            record=record,
            resolved_thread_id=state.resolved_thread_id or thread.thread_id,
            session_used_tokens=_int_or_none(state.token_usage.get("session_used_tokens")),
            user_used_tokens=_int_or_none(state.token_usage.get("user_used_tokens")),
        )


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class LoadGenerator:
    def __init__(
        self,
        *,
        config: RunConfig,
        users: list[UserState],
        recorder: RequestRecorder,
        log_monitor: LogCapMonitor,
    ) -> None:
        self.config = config
        self.users = users
        self.recorder = recorder
        self.log_monitor = log_monitor
        self.client = ChainlitProtocolClient(config)
        self.rng = random.Random(config.run_id)
        self.abort_event = asyncio.Event()
        self.reduced_logging = False
        self._recent_transport_failures: deque[bool] = deque(
            maxlen=max(self.config.failure_window_size, 1)
        )
        self._abort_reason: str | None = None
        self._consecutive_failures = 0
        self._start_gate_lock = asyncio.Lock()
        self._next_request_start_at = 0.0
        self._safety_monitor = ResourceSafetyMonitor(config)

    async def run(self) -> dict[str, Any]:
        await self.client.prelogin_users(
            self.users,
            concurrency=self.config.login_batch_size,
        )
        started_at = utcnow()
        monitor_task = asyncio.create_task(self._monitor_log_cap())
        progress_task = asyncio.create_task(self._progress_reporter(started_at))
        safety_task = asyncio.create_task(self._safety_guard())

        try:
            if self.config.is_conversation_mode:
                await self._run_conversation_mode(started_at)
            else:
                await self._run_legacy_rps_mode(started_at)
        finally:
            monitor_task.cancel()
            progress_task.cancel()
            safety_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
            with contextlib.suppress(asyncio.CancelledError):
                await safety_task

        finished_at = utcnow()
        status = self.log_monitor.check()
        return {
            "started_at": started_at,
            "finished_at": finished_at,
            "aborted_due_to_log_cap": status.abort_triggered,
            "log_cap_warning_triggered": status.warning_triggered,
            "log_size_bytes": status.size_bytes,
            "abort_reason": self._abort_reason,
        }

    async def _run_conversation_mode(self, started_at: datetime) -> None:
        semaphore = asyncio.Semaphore(max(self.config.max_global_inflight, 1))
        workers = [
            asyncio.create_task(
                self._conversation_worker(
                    user=user,
                    worker_index=index,
                    started_at=started_at,
                    semaphore=semaphore,
                )
            )
            for index, user in enumerate(self.users)
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    async def _conversation_worker(
        self,
        *,
        user: UserState,
        worker_index: int,
        started_at: datetime,
        semaphore: asyncio.Semaphore,
    ) -> None:
        if self.config.conversation_start_jitter_ms > 0:
            await asyncio.sleep(
                self.rng.uniform(
                    0.0,
                    self.config.conversation_start_jitter_ms / 1000.0,
                )
            )
        while not self.abort_event.is_set():
            if user.exhausted or self._duration_reached(started_at):
                return
            thread = self._select_conversation_thread(user)
            async with semaphore:
                if self.abort_event.is_set() or user.exhausted or self._duration_reached(started_at):
                    return
                await self._respect_conversation_start_cap()
                behavior = self._choose_conversation_behavior()
                stop_trigger_tokens = (
                    self.client.random_stop_threshold(self.rng)
                    if behavior == "stop"
                    else None
                )
                await self._run_single_request(
                    user=user,
                    thread=thread,
                    behavior=behavior,
                    session_rollover_reason=thread.pending_rollover_reason,
                    stop_trigger_tokens_estimated=stop_trigger_tokens,
                )

    async def _respect_conversation_start_cap(self) -> None:
        if self.config.target_rps <= 0:
            return
        interval_seconds = 1.0 / max(self.config.target_rps, 1)
        async with self._start_gate_lock:
            now = time.perf_counter()
            if now < self._next_request_start_at:
                await asyncio.sleep(self._next_request_start_at - now)
            current = time.perf_counter()
            self._next_request_start_at = current + interval_seconds

    def _choose_conversation_behavior(self) -> str:
        return "stop" if self.rng.random() < self.config.stop_ratio else "normal"

    async def _run_legacy_rps_mode(self, started_at: datetime) -> None:
        pending: set[asyncio.Task[None]] = set()
        next_send_at = time.perf_counter()
        round_robin_index = 0
        try:
            while True:
                all_exhausted = all(user.exhausted for user in self.users)
                duration_reached = self._duration_reached(started_at)
                if (all_exhausted and not pending) or self.abort_event.is_set():
                    break
                if duration_reached and not pending:
                    self._abort_reason = self._abort_reason or (
                        f"duration reached ({self.config.duration_seconds:.1f}s)"
                    )
                    break

                if pending:
                    await asyncio.wait(
                        pending,
                        timeout=min(max(next_send_at - time.perf_counter(), 0.0), 0.05),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    idle_sleep = min(max(next_send_at - time.perf_counter(), 0.0), 0.05)
                    if idle_sleep > 0:
                        await asyncio.sleep(idle_sleep)

                if self.abort_event.is_set() or duration_reached:
                    continue

                now = time.perf_counter()
                if now < next_send_at:
                    continue

                if len(pending) >= self.config.max_global_inflight:
                    next_send_at = now + min(
                        self._dispatch_interval_seconds(started_at),
                        0.25,
                    )
                    continue

                behavior = self.client.traffic_profile.choose_behavior(self.rng)
                candidate = self._pick_user(round_robin_index, behavior)
                if candidate is None:
                    next_send_at = now + min(
                        self._dispatch_interval_seconds(started_at),
                        0.05,
                    )
                    continue

                round_robin_index = (candidate[0] + 1) % len(self.users)
                user, thread = candidate[1], candidate[2]
                task = asyncio.create_task(
                    self._run_single_request(
                        user=user,
                        thread=thread,
                        behavior=behavior,
                        session_rollover_reason=thread.pending_rollover_reason,
                        stop_trigger_tokens_estimated=(
                            self.client.random_stop_threshold(self.rng)
                            if behavior == "stop"
                            else None
                        ),
                    )
                )
                pending.add(task)
                task.add_done_callback(pending.discard)
                next_send_at = time.perf_counter() + self._dispatch_interval_seconds(
                    started_at
                )
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _duration_reached(self, started_at: datetime) -> bool:
        if self.config.duration_seconds <= 0:
            return False
        elapsed_seconds = max((utcnow() - started_at).total_seconds(), 0.0)
        return elapsed_seconds >= self.config.duration_seconds

    def _dispatch_interval_seconds(self, started_at: datetime) -> float:
        return 1.0 / max(self._current_target_rps(started_at), 1)

    def _current_target_rps(self, started_at: datetime) -> int:
        if self.config.ramp_step_seconds <= 0:
            return max(self.config.target_rps, 1)
        ramp_start = min(max(self.config.ramp_start_rps, 1), self.config.target_rps)
        elapsed_seconds = max((utcnow() - started_at).total_seconds(), 0.0)
        ramp_stage = int(elapsed_seconds // self.config.ramp_step_seconds)
        ramped_rps = ramp_start * (2**ramp_stage)
        return max(1, min(self.config.target_rps, ramped_rps))

    def _record_failure_state(self, record: RequestRecord) -> None:
        transport_failure = record.failure_type in {
            "http_error",
            "ws_error",
            "connect_error",
            "timeout",
        }
        self._recent_transport_failures.append(transport_failure)
        if len(self._recent_transport_failures) >= self.config.failure_window_size:
            failure_ratio = sum(self._recent_transport_failures) / len(
                self._recent_transport_failures
            )
            if (
                failure_ratio >= self.config.failure_abort_ratio
                and not self.abort_event.is_set()
            ):
                self._abort_reason = (
                    "recent transport failure ratio reached "
                    f"{failure_ratio:.2f} over {len(self._recent_transport_failures)} requests"
                )
                print(f"[ERROR] QPS_test aborting safely: {self._abort_reason}", flush=True)
                self.abort_event.set()

        expected_rollover = record.quota_blocked_scope in {"thread", "user"}
        if record.success or expected_rollover:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= self.config.consecutive_failure_abort_count
            and not self.abort_event.is_set()
        ):
            self._abort_reason = (
                f"consecutive failures reached {self._consecutive_failures}"
            )
            print(f"[ERROR] QPS_test aborting safely: {self._abort_reason}", flush=True)
            self.abort_event.set()

    def _record_transport_failure(self, record: RequestRecord) -> None:
        self._record_failure_state(record)

    def _pick_user(
        self,
        start_index: int,
        behavior: str,
    ) -> tuple[int, UserState, ThreadState] | None:
        if not self.users:
            return None
        for offset in range(len(self.users)):
            index = (start_index + offset) % len(self.users)
            user = self.users[index]
            if user.exhausted:
                continue
            cap = self.config.per_user_default_inflight
            if behavior == "thread_switch":
                cap = min(max(cap, 3), self.config.per_user_max_inflight)
            if user.inflight >= cap:
                continue
            thread = self._select_legacy_thread(user, behavior)
            if thread is None:
                continue
            return index, user, thread
        return None

    def _create_thread(
        self,
        *,
        user: UserState,
        rollover_reason: str,
    ) -> ThreadState:
        rollover_index = len(user.threads) + 1
        topic_name = self.client.prompt_factory.choose_topic_name(
            user_id=user.credential.identifier,
            rollover_index=rollover_index,
        )
        thread = ThreadState(
            local_name=f"t{rollover_index:03d}",
            rollover_index=rollover_index,
            topic_name=topic_name,
            pending_rollover_reason=rollover_reason,
        )
        user.threads.append(thread)
        return thread

    def _select_conversation_thread(self, user: UserState) -> ThreadState:
        if not user.threads:
            return self._create_thread(user=user, rollover_reason="initial_thread")
        current = user.threads[-1]
        if current.exhausted:
            return self._create_thread(user=user, rollover_reason="thread_quota_blocked")
        if (
            current.thread_id is not None
            and current.session_used_tokens + self.config.minimum_request_tokens
            >= self.config.thread_limit_tokens
        ):
            current.blocked_scope = current.blocked_scope or "thread"
            current.quota_blocked_reason = (
                current.quota_blocked_reason or "predicted_thread_limit"
            )
            return self._create_thread(
                user=user,
                rollover_reason="thread_token_limit_predicted",
            )
        return current

    def _select_legacy_thread(self, user: UserState, behavior: str) -> ThreadState | None:
        reusable = [
            thread
            for thread in user.threads
            if not thread.exhausted
            and thread.active_requests == 0
            and (
                thread.thread_id is None
                or thread.session_used_tokens + self.config.minimum_request_tokens
                < self.config.thread_limit_tokens
            )
        ]

        if behavior == "thread_switch":
            desired = min(self.config.per_user_max_inflight, 3)
            active_threads = [thread for thread in user.threads if not thread.exhausted]
            while len(active_threads) < desired:
                created = self._create_thread(
                    user=user,
                    rollover_reason="manual_thread_switch",
                )
                active_threads.append(created)
                reusable.append(created)
            if reusable:
                user.last_thread_index = (user.last_thread_index + 1) % len(reusable)
                return reusable[user.last_thread_index]

        if reusable:
            reusable.sort(
                key=lambda item: (item.session_used_tokens, item.request_count),
                reverse=True,
            )
            return reusable[0]

        rollover_reason = "initial_thread" if not user.threads else "thread_token_limit_predicted"
        return self._create_thread(user=user, rollover_reason=rollover_reason)

    async def _run_single_request(
        self,
        *,
        user: UserState,
        thread: ThreadState,
        behavior: str,
        session_rollover_reason: str | None,
        stop_trigger_tokens_estimated: int | None,
    ) -> None:
        request_seq = user.next_request_seq
        conversation_turn_index = thread.request_count + 1
        user.next_request_seq += 1
        user.inflight += 1
        thread.active_requests += 1
        try:
            outcome = await self.client.run_request(
                user=user,
                thread=thread,
                request_seq=request_seq,
                behavior=behavior,
                conversation_turn_index=conversation_turn_index,
                session_rollover_reason=session_rollover_reason,
                stop_trigger_tokens_estimated=stop_trigger_tokens_estimated,
            )
            record = outcome.record
            self.recorder.record(record)
            self._record_failure_state(record)

            if outcome.resolved_thread_id:
                thread.thread_id = outcome.resolved_thread_id
            if outcome.session_used_tokens is not None:
                thread.session_used_tokens = outcome.session_used_tokens
            if outcome.user_used_tokens is not None:
                user.total_used_tokens = outcome.user_used_tokens

            thread.request_count += 1
            thread.last_updated_at = record.task_end_at
            thread.pending_rollover_reason = None

            if record.quota_blocked_scope == "thread":
                thread.blocked_scope = "thread"
                thread.quota_blocked_reason = record.quota_blocked_reason
            if record.quota_blocked_scope == "user":
                user.blocked_scope = "user"
                user.quota_blocked_reason = record.quota_blocked_reason
            if record.success:
                user.completed_requests += 1
            else:
                user.failed_requests += 1
        finally:
            user.inflight = max(user.inflight - 1, 0)
            thread.active_requests = max(thread.active_requests - 1, 0)

    async def _monitor_log_cap(self) -> None:
        while True:
            status = self.log_monitor.check()
            if status.warning_triggered and not self.reduced_logging:
                self.reduced_logging = True
                print(
                    f"[WARN] QPS_test log usage reached warning threshold: {status.size_bytes} bytes",
                    flush=True,
                )
            if status.abort_triggered:
                print(
                    f"[ERROR] QPS_test log usage reached hard limit: {status.size_bytes} bytes",
                    flush=True,
                )
                self._abort_reason = self._abort_reason or "log cap reached hard limit"
                self.abort_event.set()
                return
            await asyncio.sleep(5.0)

    async def _safety_guard(self) -> None:
        while True:
            snapshot = await self._safety_monitor.sample()
            abort_reason = self._safety_monitor.evaluate(snapshot)
            if abort_reason:
                self._abort_reason = self._abort_reason or abort_reason
                print(f"[ERROR] QPS_test aborting safely: {self._abort_reason}", flush=True)
                self.abort_event.set()
                return
            await asyncio.sleep(self.config.resource_sample_seconds)

    async def _progress_reporter(self, started_at: datetime) -> None:
        while True:
            await asyncio.sleep(self.config.progress_interval_seconds)
            total = len(self.recorder.rows)
            success = sum(1 for row in self.recorder.rows if row.success)
            failures = total - success
            exhausted = sum(1 for user in self.users if user.exhausted)
            elapsed = max((utcnow() - started_at).total_seconds(), 0.001)
            inflight = sum(user.inflight for user in self.users)
            target_value = (
                self.config.target_rps
                if self.config.is_conversation_mode
                else self._current_target_rps(started_at)
            )
            print(
                "[INFO] QPS_test progress "
                f"requests={total} success={success} fail={failures} "
                f"users_blocked={exhausted}/{len(self.users)} "
                f"avg_rps={total / elapsed:.2f} "
                f"target_rps={target_value} "
                f"inflight={inflight}/{self.config.max_global_inflight}",
                flush=True,
            )
