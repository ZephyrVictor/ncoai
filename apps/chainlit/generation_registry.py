from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import monotonic, time


@dataclass(frozen=True, slots=True)
class ActiveGeneration:
    thread_id: str
    assistant_message_id: str
    partial_output: str
    generation_status: str
    updated_at: float
    last_snapshot_at: float
    user_identifier: str | None = None
    task: asyncio.Task | None = None
    cancellation_requested: bool = False


class GenerationRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ActiveGeneration] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        thread_id: str,
        assistant_message_id: str,
        partial_output: str = "",
        generation_status: str = "pending",
        user_identifier: str | None = None,
        task: asyncio.Task | None = None,
    ) -> None:
        now = time()
        async with self._lock:
            self._entries[thread_id] = ActiveGeneration(
                thread_id=thread_id,
                assistant_message_id=assistant_message_id,
                partial_output=partial_output,
                generation_status=generation_status,
                updated_at=now,
                last_snapshot_at=0.0,
                user_identifier=user_identifier,
                task=task,
            )

    async def cancel_for_user(
        self, thread_id: str, user_identifier: str
    ) -> asyncio.Task | None:
        """Cancel an owned generation after its original socket is replaced."""
        async with self._lock:
            entry = self._entries.get(thread_id)
            if not entry or not user_identifier or entry.user_identifier != user_identifier:
                return None
            task = entry.task
            if not task or task.done() or task is asyncio.current_task():
                return None
            # A second cancel can interrupt quota settlement and resource cleanup.
            if not entry.cancellation_requested and not task.cancelling():
                task.cancel()
            self._entries[thread_id] = replace(entry, cancellation_requested=True)
            return task

    async def append_token(
        self,
        *,
        thread_id: str,
        assistant_message_id: str,
        token: str,
        generation_status: str = "streaming",
    ) -> ActiveGeneration | None:
        if not token:
            return await self.set_status(
                thread_id=thread_id,
                assistant_message_id=assistant_message_id,
                generation_status=generation_status,
            )

        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.assistant_message_id != assistant_message_id:
                return None
            updated = replace(
                entry,
                partial_output=entry.partial_output + token,
                generation_status=generation_status,
                updated_at=time(),
            )
            self._entries[thread_id] = updated
            return updated

    async def set_content(
        self,
        *,
        thread_id: str,
        assistant_message_id: str,
        partial_output: str,
        generation_status: str,
    ) -> ActiveGeneration | None:
        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.assistant_message_id != assistant_message_id:
                return None
            updated = replace(
                entry,
                partial_output=partial_output,
                generation_status=generation_status,
                updated_at=time(),
            )
            self._entries[thread_id] = updated
            return updated

    async def set_status(
        self,
        *,
        thread_id: str,
        assistant_message_id: str,
        generation_status: str,
    ) -> ActiveGeneration | None:
        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.assistant_message_id != assistant_message_id:
                return None
            updated = replace(
                entry,
                generation_status=generation_status,
                updated_at=time(),
            )
            self._entries[thread_id] = updated
            return updated

    async def get(self, thread_id: str) -> ActiveGeneration | None:
        async with self._lock:
            return self._entries.get(thread_id)

    async def mark_snapshot(self, thread_id: str) -> ActiveGeneration | None:
        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None:
                return None
            updated = replace(entry, last_snapshot_at=monotonic())
            self._entries[thread_id] = updated
            return updated

    async def snapshot_due(
        self,
        *,
        thread_id: str,
        assistant_message_id: str,
        interval_seconds: float,
    ) -> bool:
        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.assistant_message_id != assistant_message_id:
                return False
            return monotonic() - entry.last_snapshot_at >= interval_seconds

    async def remove(
        self,
        *,
        thread_id: str,
        assistant_message_id: str | None = None,
    ) -> None:
        async with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None:
                return
            if assistant_message_id and entry.assistant_message_id != assistant_message_id:
                return
            self._entries.pop(thread_id, None)
