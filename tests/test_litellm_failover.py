from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import ncoai_shared.vllm_failover as vllm_failover


TEMPLATE_TEXT = Path("infra/litellm/config.yaml").read_text(encoding="utf-8")


def test_prepare_litellm_config_without_backup_routes_primary_through_router() -> None:
    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "0",
            "CHAINLIT_OPENAI_TIMEOUT": "120",
        },
    )

    assert "qwen35b_no_think_backup" not in prepared.rendered_config
    assert "http://127.0.0.1:12234/v1" in prepared.rendered_config
    assert "model: openai/qwen35b" in prepared.rendered_config
    assert "qwen35b_no_think_lastdance" not in prepared.rendered_config
    assert "fallbacks:" not in prepared.rendered_config
    assert "set_verbose: true" in prepared.rendered_config
    assert prepared.warnings == ()


def test_prepare_litellm_config_with_backup_env_still_routes_primary_through_router(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vllm_failover,
        "probe_vllm_endpoint",
        lambda endpoint, timeout_seconds=3.0: {
            "origin": endpoint.origin,
            "api_base": endpoint.api_base,
            "health_url": endpoint.health_url,
            "models_url": endpoint.models_url,
            "checked_at": "2026-04-08T00:00:00+00:00",
            "health_ok": True,
            "models_ok": True,
            "healthy": True,
            "error": "",
        },
    )

    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "1",
            "VLLM_BACKUP_ORIGIN": "backup.example.com:12233",
            "VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS": "2.5",
            "VLLM_BACKUP_REQUEST_TIMEOUT_SECONDS": "5",
        },
    )

    assert "api_base: http://127.0.0.1:12234/v1" in prepared.rendered_config
    assert "model: openai/qwen35b" in prepared.rendered_config
    assert "qwen35b_no_think_backup" not in prepared.rendered_config
    assert "http://backup.example.com:12233/v1" not in prepared.rendered_config
    assert "qwen35b_no_think_lastdance" not in prepared.rendered_config
    assert "fallbacks:" not in prepared.rendered_config
    assert prepared.warnings == ()
    assert prepared.backup_probe is None


def test_prepare_litellm_config_ignores_broken_backup_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        vllm_failover,
        "probe_vllm_endpoint",
        lambda endpoint, timeout_seconds=3.0: {
            "origin": endpoint.origin,
            "api_base": endpoint.api_base,
            "health_url": endpoint.health_url,
            "models_url": endpoint.models_url,
            "checked_at": "2026-04-08T00:00:00+00:00",
            "health_ok": False,
            "models_ok": False,
            "healthy": False,
            "error": "timed out",
        },
    )

    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "1",
            "VLLM_BACKUP_ORIGIN": "backup.example.com:12233",
        },
    )

    assert prepared.warnings == ()
    assert prepared.backup_probe is None


def test_prepare_litellm_config_respects_verbose_env_override() -> None:
    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "0",
            "LITELLM_SET_VERBOSE": "0",
        },
    )

    assert "set_verbose: false" in prepared.rendered_config


def test_prepare_litellm_config_routes_all_models_to_lastdance_when_enabled(
    monkeypatch,
) -> None:
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("backup probe should be skipped in lastdance mode")

    monkeypatch.setattr(vllm_failover, "probe_vllm_endpoint", fail_if_called)

    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "1",
            "VLLM_BACKUP_ORIGIN": "backup.example.com:12233",
            "CHAINLIT_OPENAI_TIMEOUT": "120",
            "thisisthelastdance": "1",
        },
    )

    assert "model: openai/glm-4.5-air" in prepared.rendered_config
    assert "api_base: http://127.0.0.1:12234/v1" in prepared.rendered_config
    assert "request_timeout: 120" in prepared.rendered_config
    assert "qwen35b_no_think_backup" not in prepared.rendered_config
    assert "fallbacks:" not in prepared.rendered_config
    assert prepared.backup_probe is None
    assert prepared.warnings == ()
    assert called is False
