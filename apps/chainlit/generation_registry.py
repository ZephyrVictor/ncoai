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
            )

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
