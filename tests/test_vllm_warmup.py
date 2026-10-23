from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path("scripts/vllm/warmup.py")
MODULE_SPEC = importlib.util.spec_from_file_location("ncoai_vllm_warmup_for_tests", SCRIPT_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
warmup = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = warmup
MODULE_SPEC.loader.exec_module(warmup)


def test_warmup_checks_model_and_uses_production_prompt(monkeypatch) -> None:
    requests = []

    def fake_request(url, *, payload=None, timeout=5.0):
        requests.append((url, payload))
        if url.endswith("/v1/models"):
            return {"data": [{"id": "deployment-model"}]}
        if url.endswith("/v1/chat/completions"):
            return {"choices": [{"message": {"content": "ok"}}]}
        return {}

    monkeypatch.setattr(warmup, "request_json", fake_request)
    warmup.warmup(
        {
            "VLLM_HOST": "0.0.0.0",
            "SERVED_MODEL_NAME": "deployment-model",
            "CHAINLIT_SYSTEM_PROMPT": "Production system prompt",
        },
        ready_timeout=1,
        request_timeout=1,
    )

    assert requests[0][0] == "http://127.0.0.1:12233/health"
    payloads = [payload for _, payload in requests if payload]
    assert len(payloads) == 4
    assert all(payload["messages"][0]["content"] == "Production system prompt" for payload in payloads)
    assert payloads[1]["messages"][1] == payloads[3]["messages"][1]


def test_warmup_rejects_wrong_served_model(monkeypatch) -> None:
    monkeypatch.setattr(
        warmup,
        "request_json",
        lambda url, **kwargs: {"data": [{"id": "other-model"}]} if url.endswith("/v1/models") else {},
    )
    with pytest.raises(ValueError, match="served model"):
        warmup.wait_for_model("http://127.0.0.1:12233", "deployment-model", timeout=0.01)
