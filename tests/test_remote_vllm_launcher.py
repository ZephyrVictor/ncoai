from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/vllm/start_remote_vllm.py")
MODULE_SPEC = importlib.util.spec_from_file_location(
    "ncoai_remote_vllm_launcher_for_tests",
    SCRIPT_PATH,
)
assert MODULE_SPEC and MODULE_SPEC.loader
remote_vllm = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = remote_vllm
MODULE_SPEC.loader.exec_module(remote_vllm)


def test_remote_launcher_build_command_matches_expected_defaults() -> None:
    config = remote_vllm.LauncherConfig(
        model_path="/models/Qwen3.5-35B-A3B",
        served_model_name="qwen35b",
        host="0.0.0.0",
        port=12233,
        python_executable="python",
        cuda_visible_devices="0,1,2,3",
        tensor_parallel_size=4,
        dtype="bfloat16",
        gpu_memory_utilization=0.92,
        max_model_len=2048,
        max_num_seqs=80,
        max_num_batched_tokens=98304,
        enable_prefix_caching=True,
        enable_fp8_kv=False,
        language_model_only=True,
        enforce_eager=True,
    )

    command = remote_vllm.build_command(config)

    assert command[:3] == ["python", "-m", "vllm.entrypoints.openai.api_server"]
    assert "--enable-prefix-caching" in command
    assert "--language-model-only" in command
    assert "--enforce-eager" in command
    assert "--kv-cache-dtype" not in command


def test_remote_launcher_build_command_respects_optional_overrides() -> None:
    config = remote_vllm.LauncherConfig(
        model_path="/models/Qwen3.5-35B-A3B",
        served_model_name="qwen35b",
        host="127.0.0.1",
        port=12233,
        python_executable="/opt/venv/bin/python",
        cuda_visible_devices="0",
        tensor_parallel_size=1,
        dtype="float16",
        gpu_memory_utilization=0.8,
        max_model_len=4096,
        max_num_seqs=16,
        max_num_batched_tokens=16384,
        enable_prefix_caching=False,
        enable_fp8_kv=True,
        language_model_only=False,
        enforce_eager=False,
    )

    command = remote_vllm.build_command(config)

    assert "--enable-prefix-caching" not in command
    assert "--language-model-only" not in command
    assert "--enforce-eager" not in command
    assert "--kv-cache-dtype" in command
