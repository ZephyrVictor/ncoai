#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/middleware/litellm"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

require_env_vars \
  LITELLM_DATABASE_URL \
  LITELLM_HOST \
  LITELLM_PORT \
  LITELLM_MASTER_KEY

source "$ROOT_DIR/.venv/bin/activate"

if ! python -c "import litellm" >/dev/null 2>&1; then
  echo "[ERROR] LiteLLM runtime dependency is missing in .venv" >&2
  echo "Run: bash scripts/deploy/init-host.sh" >&2
  exit 1
fi

python "$ROOT_DIR/scripts/deploy/check_runtime.py" \
  --service litellm \
  --require-db

HOST="${LITELLM_HOST}"
PORT="${LITELLM_PORT}"
NUM_WORKERS="${LITELLM_NUM_WORKERS:-1}"
CONFIG_OUTPUT="$ROOT_DIR/.local/generated/litellm.config.yaml"

python "$ROOT_DIR/scripts/litellm/render_config.py" \
  --output "$CONFIG_OUTPUT"

exec litellm --host "$HOST" --port "$PORT" --num_workers "$NUM_WORKERS" --config "$CONFIG_OUTPUT"
