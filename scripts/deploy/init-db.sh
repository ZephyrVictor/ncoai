#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[ERROR] Missing virtualenv. Run bash scripts/deploy/init-host.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT_DIR/.venv/bin/activate"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

python "$ROOT_DIR/scripts/deploy/init_db.py"
