#!/usr/bin/env bash
# Deploy / manage 2x TP=2 Qwen3.8-27B Docker replicas on ncoai (4x RTX PRO 5000 Blackwell).
# Usage: bash scripts/vllm/start-ncoai-docker-replicas.sh [up|down|status]
set -euo pipefail

ACTION="${1:-up}"
IMAGE="${VLLM_DOCKER_IMAGE:-docker.1ms.run/vllm/vllm-openai:latest}"
MODEL_DIR="${MODEL_PATH:-/data/yacong/qwen-models/Qwen3.8-27B}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen38}"
BIND_HOST="${VLLM_DOCKER_BIND_HOST:-0.0.0.0}"
PORT0="${VLLM_REPLICA0_PORT:-18000}"
PORT1="${VLLM_REPLICA1_PORT:-18001}"
GPU_MEM_UTIL="${GPU_MEMORY_UTILIZATION:-0.94}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-2048}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-120}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"
VLLM_API_KEY="${VLLM_API_KEY:-}"
REASONING_PARSER="${VLLM_REASONING_PARSER:-qwen3}"
NAME_PREFIX="${VLLM_DOCKER_NAME_PREFIX:-ncoai-qwen38}"

case "$ACTION" in
  down)
    docker rm -f "${NAME_PREFIX}-r0" "${NAME_PREFIX}-r1" >/dev/null 2>&1 || true
    echo "[OK] Stopped ${NAME_PREFIX}-r0 and ${NAME_PREFIX}-r1"
    exit 0
    ;;
  status)
    docker ps -a --filter "name=${NAME_PREFIX}-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
    ;;
  up)
    ;;
  *)
    echo "Usage: $0 [up|down|status]" >&2
    exit 2
    ;;
esac

if [[ ! -d "$MODEL_DIR" ]]; then
  echo "[ERROR] MODEL_PATH not found: $MODEL_DIR" >&2
  exit 1
fi

docker rm -f "${NAME_PREFIX}-r0" "${NAME_PREFIX}-r1" >/dev/null 2>&1 || true

for idx in 0 1; do
  if [[ "$idx" == "0" ]]; then
    GPUS="device=0,1"
    PORT="$PORT0"
  else
    GPUS="device=2,3"
    PORT="$PORT1"
  fi
  EXTRA_ARGS=()
  if [[ -n "$REASONING_PARSER" ]]; then
    EXTRA_ARGS+=(--reasoning-parser "$REASONING_PARSER")
  fi
  if [[ -n "$VLLM_API_KEY" && "$VLLM_API_KEY" != "dummy" ]]; then
    EXTRA_ARGS+=(--api-key "$VLLM_API_KEY")
  fi
  docker run -d --name "${NAME_PREFIX}-r${idx}" \
    --restart unless-stopped \
    --gpus "\"${GPUS}\"" \
    --shm-size=16g \
    -e HF_HUB_OFFLINE=1 \
    -e VLLM_USE_DEEP_GEMM=0 \
    -p "${BIND_HOST}:${PORT}:8000" \
    -v "${MODEL_DIR}:/models/active:ro" \
    -v vllm-cache:/root/.cache/vllm \
    "$IMAGE" \
    --model /models/active \
    --served-model-name "$SERVED_MODEL_NAME" \
    --tensor-parallel-size 2 \
    --dtype bfloat16 \
    --language-model-only \
    --gpu-memory-utilization "$GPU_MEM_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
    --enable-prefix-caching \
    --disable-custom-all-reduce \
    "${EXTRA_ARGS[@]}"
done

echo "[OK] Started ${NAME_PREFIX}-r0 (${BIND_HOST}:${PORT0}) and ${NAME_PREFIX}-r1 (${BIND_HOST}:${PORT1})"
