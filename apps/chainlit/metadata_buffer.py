from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

LOGGER = logging.getLogger("ncoai.chainlit.metadata")


def merge_metadata_patch(
    base: dict[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if (
            isinstance(value, Mapping)
            and isinstance(merged.get(key), Mapping)
        ):
            merged[key] = merge_metadata_patch(
                dict(merged[key]),
                value,
            )
        else:
            merged[key] = value
    return merged


class ThreadMetadataBuffer:
    def __init__(
        self,
        *,
        data_layer: Any,
        interval_seconds: float,
    ) -> None:
        self._data_layer = data_layer
        self._interval_seconds = max(interval_seconds, 0.1)
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._run())

    async def enqueue(self, thread_id: str, patch: Mapping[str, Any]) -> None:
        if not thread_id or not patch:
            return
        async with self._lock:
            current = self._pending.get(thread_id, {})
            self._pending[thread_id] = merge_metadata_patch(current, patch)

    async def flush(self) -> None:
        async with self._lock:
            pending = self._pending
            self._pending = {}

        if not pending:
            return

        for thread_id, patch in pending.items():
            try:
                thread = await self._data_layer.get_thread(thread_id=thread_id)
                if not thread:
                    continue
                existing_metadata = thread.get("metadata") or {}
                if isinstance(existing_metadata, str):
                    try:
                        existing_metadata = json.loads(existing_metadata)
                    except json.JSONDecodeError:
                        existing_metadata = {}
                if not isinstance(existing_metadata, dict):
                    existing_metadata = {}
                await self._data_layer.update_thread(
                    thread_id=thread_id,
                    metadata=merge_metadata_patch(existing_metadata, patch),
                )
            except Exception as exc:  # pragma: no cover - operational path
                LOGGER.warning(
                    "Failed to flush metadata for thread %s: %s",
                    thread_id,
                    exc,
                )

    async def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                await self.flush()
        except asyncio.CancelledError:
            raise
