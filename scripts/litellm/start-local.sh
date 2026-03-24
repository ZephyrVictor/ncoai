#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/middleware/litellm"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

source "$ROOT_DIR/.venv/bin/activate"

export DATABASE_URL="${DATABASE_URL:-postgresql://${POSTGRES_USER:-ncoai}:${POSTGRES_PASSWORD:-ncoai_dev}@${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-ncoai}}"
DB_NAME="${DATABASE_URL##*/}"
ADMIN_DATABASE_URL="${DATABASE_URL%/*}/postgres"

if ! psql "$ADMIN_DATABASE_URL" -Atqc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  echo "[INFO] Creating database ${DB_NAME}"
  createdb --maintenance-db="$ADMIN_DATABASE_URL" "$DB_NAME" >/dev/null 2>&1 || true
fi

if ! python -c "import litellm" >/dev/null 2>&1; then
  uv pip install -e ".[proxy]"
fi

HOST="${LITELLM_HOST:-0.0.0.0}"
PORT="${LITELLM_PORT:-33455}"

exec litellm --host "$HOST" --port "$PORT" --config "$ROOT_DIR/infra/litellm/config.yaml"
