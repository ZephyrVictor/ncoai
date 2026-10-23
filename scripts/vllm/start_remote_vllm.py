#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    model_path: str
    served_model_name: str
    host: str
    port: int
    python_executable: str
    cuda_visible_devices: str
    tensor_parallel_size: int
    dtype: str
    gpu_memory_utilization: float
    max_model_len: int
    max_num_seqs: int
    max_num_batched_tokens: int
    enable_prefix_caching: bool
    enable_fp8_kv: bool
    language_model_only: bool
    enforce_eager: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start an OpenAI-compatible vLLM server with the same defaults as the local node."
    )
    parser.add_argument("--model-path", default=os.getenv("MODEL_PATH"))
    parser.add_argument("--served-model-name", default=os.getenv("SERVED_MODEL_NAME"))
    parser.add_argument("--host", default=os.getenv("VLLM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VLLM_PORT", "12233")))
    parser.add_argument("--python-executable", default=os.getenv("VLLM_PYTHON", "python"))
    parser.add_argument("--cuda-visible-devices", default=os.getenv("CUDA_VISIBLE_DEVICES", "0,1,2,3"))
    parser.add_argument("--tensor-parallel-size", type=int, default=int(os.getenv("TP_SIZE", "4")))
    parser.add_argument("--dtype", default=os.getenv("DTYPE", "bfloat16"))
    parser.add_argument("--gpu-memory-utilization", type=float, default=float(os.getenv("GPU_MEMORY_UTILIZATION", "0.90")))
    parser.add_argument("--max-model-len", type=int, default=int(os.getenv("MAX_MODEL_LEN", "2048")))
    parser.add_argument("--max-num-seqs", type=int, default=int(os.getenv("MAX_NUM_SEQS", "32")))
    parser.add_argument("--max-num-batched-tokens", type=int, default=int(os.getenv("MAX_NUM_BATCHED_TOKENS", "8192")))
    parser.add_argument("--enable-prefix-caching", action="store_true", default=env_flag("ENABLE_PREFIX_CACHING", True))
    parser.add_argument("--disable-prefix-caching", action="store_false", dest="enable_prefix_caching")
    parser.add_argument("--enable-fp8-kv", action="store_true", default=env_flag("ENABLE_FP8_KV", False))
    parser.add_argument("--disable-fp8-kv", action="store_false", dest="enable_fp8_kv")
    parser.add_argument("--language-model-only", action="store_true", default=env_flag("LANGUAGE_MODEL_ONLY", False))
    parser.add_argument("--disable-language-model-only", action="store_false", dest="language_model_only")
    parser.add_argument("--enforce-eager", action="store_true", default=env_flag("ENFORCE_EAGER", False))
    parser.add_argument("--disable-enforce-eager", action="store_false", dest="enforce_eager")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved command and exit.")
    return parser


def config_from_args(args: argparse.Namespace) -> LauncherConfig:
    return LauncherConfig(
        model_path=args.model_path,
        served_model_name=args.served_model_name,
        host=args.host,
        port=int(args.port),
        python_executable=args.python_executable,
        cuda_visible_devices=args.cuda_visible_devices,
        tensor_parallel_size=int(args.tensor_parallel_size),
        dtype=args.dtype,
        gpu_memory_utilization=float(args.gpu_memory_utilization),
        max_model_len=int(args.max_model_len),
        max_num_seqs=int(args.max_num_seqs),
        max_num_batched_tokens=int(args.max_num_batched_tokens),
        enable_prefix_caching=bool(args.enable_prefix_caching),
        enable_fp8_kv=bool(args.enable_fp8_kv),
        language_model_only=bool(args.language_model_only),
        enforce_eager=bool(args.enforce_eager),
    )


def build_command(config: LauncherConfig) -> list[str]:
    command = [
        config.python_executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--model",
        config.model_path,
        "--served-model-name",
        config.served_model_name,
        "--tensor-parallel-size",
        str(config.tensor_parallel_size),
        "--dtype",
        config.dtype,
        "--gpu-memory-utilization",
        str(config.gpu_memory_utilization),
        "--max-model-len",
        str(config.max_model_len),
        "--max-num-seqs",
        str(config.max_num_seqs),
        "--max-num-batched-tokens",
        str(config.max_num_batched_tokens),
    ]
    if config.enable_prefix_caching:
        command.append("--enable-prefix-caching")
    if config.enable_fp8_kv:
        command.extend(["--kv-cache-dtype", "fp8_e5m2"])
    if config.language_model_only:
        command.append("--language-model-only")
    if config.enforce_eager:
        command.append("--enforce-eager")
    return command


def validate_config(config: LauncherConfig) -> None:
    if not config.model_path or not config.served_model_name:
        raise SystemExit("[ERROR] MODEL_PATH and SERVED_MODEL_NAME are required.")
    model_path = Path(config.model_path)
    if not model_path.is_dir():
        raise SystemExit(f"[ERROR] MODEL_PATH does not exist: {model_path}")
    if shutil.which("nvidia-smi") is None:
        raise SystemExit("[ERROR] nvidia-smi not found. NVIDIA runtime is required.")

    try:
        check = subprocess.run(
            [config.python_executable, "-c", "import vllm"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            f"[ERROR] Python executable not found: {config.python_executable}"
        ) from exc
    if check.returncode != 0:
        raise SystemExit(
            "[ERROR] vllm is not installed in the selected Python environment."
        )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = config_from_args(args)
    if not config.model_path or not config.served_model_name:
        parser.error("--model-path and --served-model-name are required (or set MODEL_PATH and SERVED_MODEL_NAME)")
    command = build_command(config)

    print("[INFO] Starting remote vLLM server with:")
    print(f"[INFO] MODEL_PATH={config.model_path}")
    print(f"[INFO] PYTHON={config.python_executable}")
    print(f"[INFO] CUDA_VISIBLE_DEVICES={config.cuda_visible_devices}")
    print(
        "[INFO] TP_SIZE="
        f"{config.tensor_parallel_size}, MAX_MODEL_LEN={config.max_model_len}, "
        f"MAX_NUM_SEQS={config.max_num_seqs}"
    )
    print(
        "[INFO] GPU_MEMORY_UTILIZATION="
        f"{config.gpu_memory_utilization}, MAX_NUM_BATCHED_TOKENS={config.max_num_batched_tokens}"
    )
    print(f"[INFO] Command: {shlex.join(command)}")

    if args.dry_run:
        return 0

    validate_config(config)
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = config.cuda_visible_devices
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    raise SystemExit(main())
