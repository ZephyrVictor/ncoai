from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path("apps/chainlit/settings.py")
MODULE_SPEC = importlib.util.spec_from_file_location("ncoai_chainlit_settings_for_tests", SCRIPT_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
settings_module = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = settings_module
MODULE_SPEC.loader.exec_module(settings_module)


def test_default_admission_matches_vllm_starting_profile(monkeypatch) -> None:
    for name in (
        "MAX_NUM_SEQS",
        "CHAINLIT_GLOBAL_ADMISSION_MAX_INFLIGHT",
        "CHAINLIT_GLOBAL_ADMISSION_QUEUE_TIMEOUT_SECONDS",
        "CHAINLIT_LITELLM_QUEUE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = settings_module.load_settings()
    assert settings.admission.max_inflight == 32
    assert settings.admission.queue_timeout_seconds == 120
    assert settings.litellm.queue_timeout_seconds == 120


def test_admission_tracks_custom_vllm_limit(monkeypatch) -> None:
    monkeypatch.setenv("MAX_NUM_SEQS", "64")
    monkeypatch.delenv("CHAINLIT_GLOBAL_ADMISSION_MAX_INFLIGHT", raising=False)

    settings = settings_module.load_settings()
    assert settings.admission.max_inflight == 64
