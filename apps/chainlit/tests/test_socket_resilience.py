from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import chainlit.socket as chainlit_socket  # noqa: E402
from chainlit.user import PersistedUser  # noqa: E402


@pytest.mark.asyncio
async def test_connect_treats_orphaned_thread_as_non_resumable(monkeypatch) -> None:
    user = PersistedUser(
        id="user-db-1",
        identifier="demo-user",
        createdAt="2026-04-18T00:00:00Z",
        metadata={},
    )
    data_layer = SimpleNamespace(get_thread_author=AsyncMock(side_effect=ValueError("orphaned")))

    monkeypatch.setattr(chainlit_socket, "require_login", lambda: True)
    monkeypatch.setattr(
        chainlit_socket,
        "_authenticate_connection",
        AsyncMock(return_value=(user, "token")),
    )
    monkeypatch.setattr(chainlit_socket, "get_data_layer", lambda: data_layer)
    monkeypatch.setattr(
        chainlit_socket,
        "restore_existing_session",
        lambda *args, **kwargs: True,
    )

    connected = await chainlit_socket.connect(
        "sid-1",
        {},
        {
            "sessionId": "session-1",
            "userEnv": None,
            "clientType": "webapp",
            "chatProfile": None,
            "threadId": "thread-1",
        },
    )

    assert connected is True
