#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/apps/chainlit"
VENDOR_DIR="$APP_DIR/vendor/chainlit"
TOOLCHAIN_DIR="$ROOT_DIR/.local/toolchain"
NODE_VERSION="${CHAINLIT_NODE_VERSION:-v20.20.1}"
NODE_DIR="$TOOLCHAIN_DIR/node-${NODE_VERSION}-linux-x64"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

mkdir -p "$TOOLCHAIN_DIR"

if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  echo "[INFO] Installing local Node.js ${NODE_VERSION}"
  cd "$TOOLCHAIN_DIR"
  curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/$(basename "$NODE_DIR").tar.xz"
  tar -xf "$(basename "$NODE_DIR").tar.xz"
fi

export PATH="$NODE_DIR/bin:$PATH"
corepack enable
corepack prepare pnpm@9.15.9 --activate >/dev/null

source "$ROOT_DIR/.venv/bin/activate"

export DATABASE_URL="${DATABASE_URL:-postgresql://${POSTGRES_USER:-ncoai}:${POSTGRES_PASSWORD:-ncoai_dev}@${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-ncoai}}"
export CHAINLIT_APP_ROOT="$APP_DIR"
export CHAINLIT_HOST="${CHAINLIT_HOST:-0.0.0.0}"
export CHAINLIT_PORT="${CHAINLIT_PORT:-3100}"
export CHAINLIT_ROOT_PATH="${CHAINLIT_ROOT_PATH:-/chat}"
export CHAINLIT_AUTH_SECRET="${CHAINLIT_AUTH_SECRET:-chainlit-local-dev-secret-2026-acceptance}"
export CHAINLIT_LITELLM_BASE_URL="${CHAINLIT_LITELLM_BASE_URL:-http://127.0.0.1:33455/v1}"
export CHAINLIT_LITELLM_API_KEY="${CHAINLIT_LITELLM_API_KEY:-${LITELLM_MASTER_KEY:-sk-local-dev}}"
export CHAINLIT_APP_USERNAME="${CHAINLIT_APP_USERNAME:-acceptance}"
export CHAINLIT_APP_PASSWORD="${CHAINLIT_APP_PASSWORD:-acceptance123}"

DB_NAME="${DATABASE_URL##*/}"
ADMIN_DATABASE_URL="${DATABASE_URL%/*}/postgres"
if ! psql "$ADMIN_DATABASE_URL" -Atqc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  echo "[INFO] Creating database ${DB_NAME}"
  createdb --maintenance-db="$ADMIN_DATABASE_URL" "$DB_NAME" >/dev/null 2>&1 || true
fi

if [[ ! -d "$VENDOR_DIR/backend/chainlit/frontend/dist" || ! -d "$VENDOR_DIR/backend/chainlit/copilot/dist" ]] || ! python -c "import chainlit, openai, playwright, redis, requests, tiktoken" >/dev/null 2>&1; then
  echo "[INFO] Installing Chainlit runtime dependencies"
  uv pip install openai playwright redis requests tiktoken -e "${VENDOR_DIR}/backend[custom-data]"
fi

echo "[INFO] Starting Chainlit on http://${CHAINLIT_HOST}:${CHAINLIT_PORT}${CHAINLIT_ROOT_PATH}"
exec chainlit run "$APP_DIR/app.py" \
  --headless \
  --host "$CHAINLIT_HOST" \
  --port "$CHAINLIT_PORT" \
  --root-path "$CHAINLIT_ROOT_PATH"
