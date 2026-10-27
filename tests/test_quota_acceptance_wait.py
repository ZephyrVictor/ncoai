from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/admin_e2e_quota_v2.py"
SPEC = importlib.util.spec_from_file_location("quota_acceptance_for_tests", SCRIPT_PATH)
assert SPEC and SPEC.loader
quota_e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quota_e2e
SPEC.loader.exec_module(quota_e2e)


@pytest.mark.parametrize(
    ("states", "expected_blocked"),
    [
        # A reservation can temporarily block quota while output is still streaming.
        ([(1520, "blocked", True, 0), (0, "ok", False, 1450)], False),
        # Settlement can finish before the browser receives the last stream event.
        ([(0, "blocked", True, 0), (0, "blocked", False, 1450)], False),
        # A settled response that exhausts quota is still a completed response.
        ([(0, "blocked", False, 1450)], False),
        # A rejected prompt has neither a reservation nor new usage.
        ([(0, "blocked", False, 0)], True),
    ],
)
def test_send_waits_for_settlement_and_stream_end(monkeypatch, states, expected_blocked):
    pending = iter(states)
    current = {}
    calls = []

    def fetch(_page):
        reserved, status, streaming, used = next(pending)
        current.update(streaming=streaming, used=used)
        calls.append(status)
        return {
            "status": 200,
            "body": {"quota": {"reserved_tokens": reserved, "status": status}},
        }

    page = SimpleNamespace(
        locator=lambda _selector: SimpleNamespace(
            is_visible=lambda: current["streaming"]
        )
    )
    monkeypatch.setattr(quota_e2e, "quota_fetch", fetch)
    monkeypatch.setattr(quota_e2e, "latest_footer_text", lambda _page: "footer")
    monkeypatch.setattr(quota_e2e, "footer_used_tokens", lambda _page: current["used"])
    monkeypatch.setattr(quota_e2e, "latest_token_usage_text", lambda _page: "usage")
    monkeypatch.setattr(quota_e2e, "blocked_text", lambda _page: None)
    monkeypatch.setattr(quota_e2e.time, "sleep", lambda _seconds: None)

    result = quota_e2e.wait_for_send_result(page, previous_thread_used=0)

    assert len(calls) == len(states)
    assert result["blocked"] is expected_blocked
    assert result["thread_used_tokens"] == states[-1][-1]


@pytest.mark.parametrize("disabled", [False, True])
def test_capture_checks_existing_composer_block(monkeypatch, disabled):
    actions = []
    page = SimpleNamespace(
        locator=lambda selector: SimpleNamespace(
            is_disabled=lambda: disabled,
            fill=lambda prompt: actions.append((selector, prompt)),
            click=lambda: actions.append((selector, "click")),
        )
    )
    monkeypatch.setattr(
        quota_e2e,
        "wait_for_send_result",
        lambda _page, **_kwargs: {"blocked": disabled},
    )

    result = quota_e2e.send_prompt_and_capture(
        page, prompt="continue", previous_thread_used=123
    )

    assert result == {"blocked": disabled, "submitted": not disabled}
    expected_actions = [] if disabled else [
        ("#chat-input", "continue"),
        ("#chat-submit", "click"),
    ]
    assert actions == expected_actions
