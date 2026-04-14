#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/apps/chainlit"
VENDOR_DIR="$APP_DIR/vendor/chainlit"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

require_env_vars \
  DATABASE_URL \
  REDIS_URL \
  CHAINLIT_HOST \
  CHAINLIT_PORT \
  CHAINLIT_ROOT_PATH \
  CHAINLIT_AUTH_SECRET \
  CHAINLIT_LITELLM_BASE_URL \
  CHAINLIT_LITELLM_API_KEY

source "$ROOT_DIR/.venv/bin/activate"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"
unset DEBUG

export CHAINLIT_APP_ROOT="$APP_DIR"
export CHAINLIT_DEBUG="${CHAINLIT_DEBUG:-false}"

if [[ ! -d "$VENDOR_DIR/backend/chainlit/frontend/dist" || ! -d "$VENDOR_DIR/backend/chainlit/copilot/dist" ]] || ! python -c "import chainlit, openai, playwright, prometheus_client, pyftpdlib, redis, requests, tiktoken" >/dev/null 2>&1; then
  echo "[ERROR] Chainlit runtime dependencies or built frontend assets are missing" >&2
  echo "Run: bash scripts/deploy/init-host.sh" >&2
  exit 1
fi

python "$ROOT_DIR/scripts/deploy/check_runtime.py" \
  --service chainlit \
  --require-db \
  --require-redis

echo "[INFO] Starting Chainlit on http://${CHAINLIT_HOST}:${CHAINLIT_PORT}${CHAINLIT_ROOT_PATH}"
exec chainlit run "$APP_DIR/app.py" \
  --headless \
  --host "$CHAINLIT_HOST" \
  --port "$CHAINLIT_PORT" \
  --root-path "$CHAINLIT_ROOT_PATH"
