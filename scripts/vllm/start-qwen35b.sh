#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

require_env_vars \
  MODEL_PATH \
  SERVED_MODEL_NAME \
  VLLM_HOST \
  VLLM_PORT \
  CUDA_VISIBLE_DEVICES \
  TP_SIZE \
  DTYPE \
  GPU_MEMORY_UTILIZATION \
  MAX_MODEL_LEN \
  MAX_NUM_SEQS \
  MAX_NUM_BATCHED_TOKENS

MODEL_PATH="${MODEL_PATH}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME}"
HOST="${VLLM_HOST}"
PORT="${VLLM_PORT}"

if [[ -z "${VLLM_PYTHON:-}" && -x "$ROOT_DIR/.venv/bin/python" ]]; then
  VLLM_PYTHON="$ROOT_DIR/.venv/bin/python"
fi
VLLM_PYTHON="${VLLM_PYTHON:-python}"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}"
TP_SIZE="${TP_SIZE}"

DTYPE="${DTYPE}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION}"
MAX_MODEL_LEN="${MAX_MODEL_LEN}"
MAX_NUM_SEQS="${MAX_NUM_SEQS}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS}"

ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"
ENABLE_FP8_KV="${ENABLE_FP8_KV:-0}"
LANGUAGE_MODEL_ONLY="${LANGUAGE_MODEL_ONLY:-0}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"

if [[ ! -d "$MODEL_PATH" ]]; then
  echo "[ERROR] MODEL_PATH does not exist: $MODEL_PATH" >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[ERROR] nvidia-smi not found. NVIDIA driver/runtime is required." >&2
  exit 1
fi

if ! "$VLLM_PYTHON" -c "import vllm" >/dev/null 2>&1; then
  echo "[ERROR] vllm is not installed in current Python environment." >&2
  echo "Try: uv pip install --python $ROOT_DIR/.venv/bin/python -U vllm" >&2
  exit 1
fi

ARGS=(
  --host "$HOST"
  --port "$PORT"
  --model "$MODEL_PATH"
  --served-model-name "$SERVED_MODEL_NAME"
  --tensor-parallel-size "$TP_SIZE"
  --dtype "$DTYPE"
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
)

if [[ "$ENABLE_PREFIX_CACHING" == "1" ]]; then
  ARGS+=(--enable-prefix-caching)
fi

if [[ "$ENABLE_FP8_KV" == "1" ]]; then
  ARGS+=(--kv-cache-dtype fp8_e5m2)
fi

if [[ "$LANGUAGE_MODEL_ONLY" == "1" ]]; then
  ARGS+=(--language-model-only)
fi

if [[ "$ENFORCE_EAGER" == "1" ]]; then
  ARGS+=(--enforce-eager)
fi

echo "[INFO] Starting vLLM OpenAI-compatible server"
echo "[INFO] MODEL_PATH=$MODEL_PATH"
echo "[INFO] VLLM_PYTHON=$VLLM_PYTHON"
echo "[INFO] CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "[INFO] TP_SIZE=$TP_SIZE, MAX_MODEL_LEN=$MAX_MODEL_LEN, MAX_NUM_SEQS=$MAX_NUM_SEQS"
echo "[INFO] GPU_MEMORY_UTILIZATION=$GPU_MEMORY_UTILIZATION, MAX_NUM_BATCHED_TOKENS=$MAX_NUM_BATCHED_TOKENS"

export CUDA_VISIBLE_DEVICES
exec "$VLLM_PYTHON" -m vllm.entrypoints.openai.api_server "${ARGS[@]}"
