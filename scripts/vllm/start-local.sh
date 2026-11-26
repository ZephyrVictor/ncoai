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
  VLLM_PORT

MODEL_PATH="${MODEL_PATH}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME}"
HOST="${VLLM_HOST}"
PORT="${VLLM_PORT}"

if [[ -z "${VLLM_PYTHON:-}" && -x "$ROOT_DIR/.venv/bin/python" ]]; then
  VLLM_PYTHON="$ROOT_DIR/.venv/bin/python"
fi
VLLM_PYTHON="${VLLM_PYTHON:-python}"

if [[ -x "$HOME/.local/bin/ninja" ]]; then
  export PATH="$HOME/.local/bin:$PATH"
fi

# FlashInfer's sampler JIT requires a newer nvcc than some supported hosts provide.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3}"
TP_SIZE="${TP_SIZE:-4}"

DTYPE="${DTYPE:-bfloat16}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-2048}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-32}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"

ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"
ENABLE_FP8_KV="${ENABLE_FP8_KV:-0}"
LANGUAGE_MODEL_ONLY="${LANGUAGE_MODEL_ONLY:-0}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"

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

if [[ -n "${VLLM_REPLICA_GPU_GROUPS:-}" ]]; then
  exec "$VLLM_PYTHON" "$ROOT_DIR/scripts/vllm/start_replicas.py"
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
