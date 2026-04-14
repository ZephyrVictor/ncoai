#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/apps/admin"
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
  ADMIN_APP_HOST \
  ADMIN_APP_PORT \
  ADMIN_APP_ROOT_PATH \
  ADMIN_APP_SESSION_SECRET \
  ADMIN_APP_USERNAME \
  ADMIN_APP_PASSWORD

source "$ROOT_DIR/.venv/bin/activate"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

if ! python -c "import fastapi, uvicorn, jinja2, itsdangerous, bcrypt, multipart, prometheus_client, sqlalchemy" >/dev/null 2>&1; then
  echo "[ERROR] Admin runtime dependencies are missing in .venv" >&2
  echo "Run: bash scripts/deploy/init-host.sh" >&2
  exit 1
fi

python "$ROOT_DIR/scripts/deploy/check_runtime.py" \
  --service admin \
  --require-db \
  --require-redis

echo "[INFO] Starting admin app on http://${ADMIN_APP_HOST}:${ADMIN_APP_PORT}${ADMIN_APP_ROOT_PATH}"
exec uvicorn app:app \
  --app-dir "$APP_DIR" \
  --host "$ADMIN_APP_HOST" \
  --port "$ADMIN_APP_PORT"
