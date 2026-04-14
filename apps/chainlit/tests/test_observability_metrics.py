from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as chainlit_app  # noqa: E402


def test_metrics_routes_are_registered() -> None:
    for path in chainlit_app.chainlit_api_paths("/metrics"):
        assert chainlit_app.chainlit_route_exists(path, "GET")


@pytest.mark.asyncio
async def test_metrics_response_contains_custom_counters() -> None:
    chainlit_app.APP_METRICS.record_auth_attempt("team-token", "success")
    chainlit_app.APP_METRICS.record_quota_event("thread", "blocked")
    chainlit_app.APP_METRICS.record_upstream_request("litellm", "success")

    response = await chainlit_app.get_metrics_response()
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "ncoai_auth_attempts_total" in body
    assert 'source="team-token"' not in body
    assert 'source="team_token"' in body
    assert 'scope="thread"' in body
    assert 'upstream="litellm"' in body
