from __future__ import annotations

import urllib.error
from contextlib import nullcontext

import pytest

import devctl


def test_start_waits_for_readiness_by_default():
    assert devctl.should_wait_for_readiness({})
    assert devctl.should_wait_for_readiness({'DEVCTL_WAIT_FOR_READINESS': '1'})


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'off'])
def test_readiness_wait_can_be_explicitly_disabled(value):
    assert not devctl.should_wait_for_readiness({'DEVCTL_WAIT_FOR_READINESS': value})


def test_litellm_readiness_authenticates_and_retries_transient_failure(monkeypatch):
    requests = []
    sleeps = []

    def open_request(request, timeout):
        requests.append(request)
        if len(requests) <= 2:
            raise urllib.error.HTTPError(request.full_url, 503, 'starting', {}, None)
        return nullcontext(type('Response', (), {'status': 200})())

    monkeypatch.setattr(devctl, 'load_env', lambda mode: {'LITELLM_MASTER_KEY': 'test-readiness-key'})
    monkeypatch.setattr(devctl.urllib.request, 'urlopen', open_request)
    monkeypatch.setattr(devctl.time, 'sleep', lambda seconds: sleeps.append(seconds))

    assert devctl.wait_for_service_readiness('litellm')
    assert requests[0].full_url.endswith('/health/readiness')
    assert all(r.get_header('Authorization') == 'Bearer test-readiness-key' for r in requests)
    assert sleeps == [1.0]


def test_start_runtime_waits_for_http_before_reporting_success(monkeypatch):
    events = []
    states = iter([None, {'pid': 123, 'tmux_target': 'test:admin'}])
    monkeypatch.setattr(devctl, 'load_running_state', lambda name: next(states))
    monkeypatch.setattr(devctl, 'list_windows', lambda: [])
    monkeypatch.setattr(devctl, 'env_overrides_for_runtime', lambda *args: {})
    monkeypatch.setattr(devctl, 'build_tmux_command', lambda *args: 'test-command')
    monkeypatch.setattr(devctl, 'session_exists', lambda: False)
    monkeypatch.setattr(devctl, 'tmux_run', lambda *args, **kwargs: None)
    monkeypatch.setattr(devctl, 'wait_for_service_readiness', lambda *args, **kwargs: events.append('http-ready') or True)

    assert devctl.start_runtime('admin', 'admin', {}) == 0
    assert events == ['http-ready']


def test_failed_http_readiness_stops_new_runtime(monkeypatch):
    events = []
    states = iter([None, {'pid': 123}])
    monkeypatch.setattr(devctl, 'load_running_state', lambda name: next(states))
    monkeypatch.setattr(devctl, 'list_windows', lambda: [])
    monkeypatch.setattr(devctl, 'env_overrides_for_runtime', lambda *args: {})
    monkeypatch.setattr(devctl, 'build_tmux_command', lambda *args: 'test-command')
    monkeypatch.setattr(devctl, 'session_exists', lambda: False)
    monkeypatch.setattr(devctl, 'tmux_run', lambda *args, **kwargs: None)
    monkeypatch.setattr(devctl, 'wait_for_service_readiness', lambda *args, **kwargs: False)
    monkeypatch.setattr(devctl, 'stop_runtime', lambda *args: events.append('stopped'))
    monkeypatch.setattr(devctl, 'tail_text', lambda *args, **kwargs: '')

    assert devctl.start_runtime('admin', 'admin', {}) == 1
    assert events == ['stopped']
