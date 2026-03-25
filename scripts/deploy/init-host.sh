#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

VENV_DIR="$ROOT_DIR/.venv"
TOOLCHAIN_DIR="$ROOT_DIR/.local/toolchain"
NODE_VERSION="${CHAINLIT_NODE_VERSION:-v20.20.1}"
NODE_DIR="$TOOLCHAIN_DIR/node-${NODE_VERSION}-linux-x64"
CHAINLIT_VENDOR_DIR="$ROOT_DIR/apps/chainlit/vendor/chainlit"
FRONTEND_DIST_DIR="$CHAINLIT_VENDOR_DIR/backend/chainlit/frontend/dist"
COPILOT_DIST_DIR="$CHAINLIT_VENDOR_DIR/backend/chainlit/copilot/dist"
BUILD_CHAINLIT_FRONTEND="${BUILD_CHAINLIT_FRONTEND:-auto}"
ALLOW_NODE_DOWNLOAD="${ALLOW_NODE_DOWNLOAD:-1}"
INSTALL_PLAYWRIGHT_BROWSERS="${INSTALL_PLAYWRIGHT_BROWSERS:-1}"
UV_PIP_EXTRA_ARGS="${UV_PIP_EXTRA_ARGS:-}"

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] Required command not found: $cmd" >&2
    exit 1
  fi
}

need_cmd bash
need_cmd tmux
need_cmd docker
need_cmd python3
need_cmd uv
need_cmd nvidia-smi

if [[ -z "${MODEL_PATH:-}" || ! -d "${MODEL_PATH}" ]]; then
  echo "[ERROR] MODEL_PATH is missing or does not exist: ${MODEL_PATH:-<unset>}" >&2
  exit 1
fi

mkdir -p "$TOOLCHAIN_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "[INFO] Creating virtualenv at $VENV_DIR"
  uv venv "$VENV_DIR" --python 3.11
fi

if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  if [[ "$BUILD_CHAINLIT_FRONTEND" != "0" && "$ALLOW_NODE_DOWNLOAD" == "1" ]]; then
    need_cmd curl
    need_cmd tar
    echo "[INFO] Installing local Node.js ${NODE_VERSION}"
    mkdir -p "$TOOLCHAIN_DIR"
    cd "$TOOLCHAIN_DIR"
    curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/$(basename "$NODE_DIR").tar.xz"
    tar -xf "$(basename "$NODE_DIR").tar.xz"
  fi
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

echo "[INFO] Installing Python dependencies into $VENV_DIR"
read -r -a UV_PIP_EXTRA_ARGS_ARR <<< "$UV_PIP_EXTRA_ARGS"
uv pip install \
  "${UV_PIP_EXTRA_ARGS_ARR[@]}" \
  asyncpg \
  hatchling \
  "psycopg[binary]" \
  playwright \
  redis \
  requests \
  tiktoken \
  -e "$ROOT_DIR/apps/admin" \
  -e "$ROOT_DIR/apps/chainlit" \
  -e "$ROOT_DIR/apps/chainlit/vendor/chainlit/backend[custom-data]" \
  -e "$ROOT_DIR/middleware/litellm[proxy]"

if [[ "$INSTALL_PLAYWRIGHT_BROWSERS" == "1" ]]; then
  echo "[INFO] Installing Playwright Chromium browser"
  python -m playwright install chromium
fi

if [[ "$BUILD_CHAINLIT_FRONTEND" == "0" ]]; then
  echo "[INFO] Skipping Chainlit frontend build because BUILD_CHAINLIT_FRONTEND=0"
elif [[ "$BUILD_CHAINLIT_FRONTEND" == "auto" && -d "$FRONTEND_DIST_DIR" && -d "$COPILOT_DIST_DIR" && ! -x "$NODE_DIR/bin/node" ]] && ! command -v node >/dev/null 2>&1; then
  echo "[INFO] Reusing existing Chainlit frontend assets; Node.js is not installed and auto-build is disabled"
else
  if [[ -x "$NODE_DIR/bin/node" ]]; then
    export PATH="$NODE_DIR/bin:$PATH"
  elif command -v node >/dev/null 2>&1; then
    export PATH="$(dirname "$(command -v node)"):$PATH"
  else
    echo "[ERROR] Node.js is required to build Chainlit frontend assets." >&2
    echo "[ERROR] Provide system Node, allow ALLOW_NODE_DOWNLOAD=1, or set BUILD_CHAINLIT_FRONTEND=0 with prebuilt dist assets." >&2
    exit 1
  fi

  corepack enable
  corepack prepare pnpm@9.15.9 --activate >/dev/null
  echo "[INFO] Building Chainlit frontend assets"
  python "$CHAINLIT_VENDOR_DIR/backend/build.py"
fi

echo "[INFO] Host initialization completed"
