#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/apps/lastdance_proxy"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

source "$ROOT_DIR/.venv/bin/activate"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

if ! python -c "import fastapi, httpx, uvicorn, prometheus_client" >/dev/null 2>&1; then
  echo "[ERROR] Lastdance runtime dependencies are missing in .venv" >&2
  echo "Run: bash scripts/deploy/init-host.sh" >&2
  exit 1
fi

echo "[INFO] Starting lastdance-proxy on http://127.0.0.1:12234"
exec uvicorn app:app \
  --app-dir "$APP_DIR" \
  --host 127.0.0.1 \
  --port 12234
