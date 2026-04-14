from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import ncoai_shared.vllm_failover as vllm_failover


TEMPLATE_TEXT = Path("infra/litellm/config.yaml").read_text(encoding="utf-8")


def test_prepare_litellm_config_without_backup_keeps_primary_only() -> None:
    prepared = vllm_failover.prepare_litellm_config(
        TEMPLATE_TEXT,
        env={
            "VLLM_HOST": "127.0.0.1",
            "VLLM_PORT": "12233",
            "SERVED_MODEL_NAME": "qwen35b",
            "VLLM_FAILOVER_ENABLED": "0",
        },
    )

    assert "qwen35b_no_think_backup" not in prepared.rendered_config
    assert "fallbacks:" not in prepared.rendered_config
    assert "set_verbose: true" in prepared.rendered_config
    assert prepared.warnings == ()


def test_prepare_litellm_config_with_backup_renders_fallback_models(
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

    assert "qwen35b_no_think_backup" in prepared.rendered_config
    assert "http://backup.example.com:12233/v1" in prepared.rendered_config
    assert "fallbacks:" in prepared.rendered_config
    assert prepared.warnings == ()


def test_prepare_litellm_config_warns_when_backup_probe_fails(monkeypatch) -> None:
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

    assert prepared.warnings
    assert "currently unreachable" in prepared.warnings[0]


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
