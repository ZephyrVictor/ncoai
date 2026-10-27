from __future__ import annotations

import devctl


def test_apps_alias_expands_to_start_order() -> None:
    assert devctl.normalize_services(["apps"], devctl.DEFAULT_START_ORDER) == list(devctl.DEFAULT_START_ORDER)


def test_up_apps_skips_disabled_caps(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(devctl, "load_env", lambda mode: {"CAPS_ENABLED": "0"})
    monkeypatch.setattr(devctl, "has_tmux", lambda: True)
    monkeypatch.setattr(devctl, "preflight_service_checks", lambda services: 0)
    monkeypatch.setattr(devctl, "start_service", lambda service: events.append(service) or 0)
    monkeypatch.setattr(devctl, "warmup_vllm", lambda: 0)

    assert devctl.start(["apps"]) == 0
    assert "caps-ftp" not in events


def test_up_warms_vllm_before_litellm(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(devctl, "has_tmux", lambda: True)
    monkeypatch.setattr(devctl, "preflight_service_checks", lambda services: 0)
    monkeypatch.setattr(devctl, "start_service", lambda service: events.append(service) or 0)
    monkeypatch.setattr(devctl, "warmup_vllm", lambda: events.append("warmup") or 0)

    assert devctl.start(["vllm", "litellm"]) == 0
    assert events == ["vllm", "warmup", "litellm"]


def test_up_downstream_only_requires_warmup(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(devctl, "has_tmux", lambda: True)
    monkeypatch.setattr(devctl, "preflight_service_checks", lambda services: 0)
    monkeypatch.setattr(devctl, "start_service", lambda service: events.append(service) or 0)
    monkeypatch.setattr(devctl, "warmup_vllm", lambda: events.append("warmup") or 1)

    assert devctl.start(["litellm"]) == 1
    assert events == ["warmup"]


def test_up_single_chainlit_worker_requires_warmup(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(devctl, "has_tmux", lambda: True)
    monkeypatch.setattr(devctl, "preflight_service_checks", lambda services: 0)
    monkeypatch.setattr(devctl, "start_service", lambda service: events.append(service) or 0)
    monkeypatch.setattr(devctl, "warmup_vllm", lambda: events.append("warmup") or 0)

    assert devctl.start(["chainlit-1"]) == 0
    assert events == ["warmup", "chainlit-1"]


def test_up_admin_does_not_wait_for_vllm(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(devctl, "has_tmux", lambda: True)
    monkeypatch.setattr(devctl, "preflight_service_checks", lambda services: 0)
    monkeypatch.setattr(devctl, "start_service", lambda service: events.append(service) or 0)
    monkeypatch.setattr(devctl, "warmup_vllm", lambda: events.append("warmup") or 0)

    assert devctl.start(["admin"]) == 0
    assert events == ["admin"]
