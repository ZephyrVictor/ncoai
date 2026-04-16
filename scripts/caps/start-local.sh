#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/apps/chainlit"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
  echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
  exit 1
fi

require_env_vars \
  CAPS_ENABLED \
  CAPS_STORAGE_ROOT \
  CAPS_FTP_HOST \
  CAPS_FTP_PORT \
  CAPS_FTP_PASSIVE_PORT_START \
  CAPS_FTP_PASSIVE_PORT_END \
  GZCTF_FIXED_GAME_ID

source "$ROOT_DIR/.venv/bin/activate"
export PYTHONPATH="$APP_DIR:$ROOT_DIR:${PYTHONPATH:-}"
unset DEBUG

if ! python -c "import pyftpdlib" >/dev/null 2>&1; then
  echo "[ERROR] CAPS FTP runtime dependency is missing: pyftpdlib" >&2
  echo "Run: bash scripts/deploy/init-host.sh" >&2
  exit 1
fi

echo "[INFO] Starting CAPS FTP on ${CAPS_FTP_HOST}:${CAPS_FTP_PORT}"
exec python "$APP_DIR/caps_service.py"
