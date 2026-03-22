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

if ! python -c "import litellm" >/dev/null 2>&1; then
  uv pip install -e ".[proxy]"
fi

HOST="${LITELLM_HOST:-127.0.0.1}"
PORT="${LITELLM_PORT:-4000}"

exec litellm --host "$HOST" --port "$PORT" --config "$ROOT_DIR/infra/litellm/config.yaml"
