#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OPENWEBUI_DIR="$ROOT_DIR/middleware/open-webui"
OPENWEBUI_BACKEND_DIR="$OPENWEBUI_DIR/backend"

cd "$OPENWEBUI_BACKEND_DIR"

if ! command -v uv >/dev/null 2>&1; then
	echo "[ERROR] uv is required. Install it first: https://docs.astral.sh/uv/" >&2
	exit 1
fi

WEBUI_HOST="${OPENWEBUI_HOST:-127.0.0.1}"
WEBUI_PORT="${OPENWEBUI_PORT:-8080}"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
	echo "[ERROR] Root virtualenv is missing: $ROOT_DIR/.venv" >&2
	echo "Create it with: uv venv $ROOT_DIR/.venv --python 3.11" >&2
	exit 1
fi

source "$ROOT_DIR/.venv/bin/activate"

if ! PYTHONPATH="$OPENWEBUI_BACKEND_DIR" python -c "import open_webui, fastapi, ddgs" >/dev/null 2>&1; then
	uv pip install --python "$ROOT_DIR/.venv/bin/python" -r requirements.txt
fi

# Force OpenAI-compatible backend through local LiteLLM
export ENABLE_OLLAMA_API="${ENABLE_OLLAMA_API:-false}"
export ENABLE_OPENAI_API="${ENABLE_OPENAI_API:-true}"
export OPENAI_API_BASE_URLS="${OPENAI_API_BASE_URLS:-http://127.0.0.1:4000/v1}"
export OPENAI_API_KEYS="${OPENAI_API_KEYS:-sk-local-dev}"
export OFFLINE_MODE="${OFFLINE_MODE:-true}"
export BYPASS_EMBEDDING_AND_RETRIEVAL="${BYPASS_EMBEDDING_AND_RETRIEVAL:-true}"
export RAG_EMBEDDING_MODEL_AUTO_UPDATE="${RAG_EMBEDDING_MODEL_AUTO_UPDATE:-false}"
export PYTHONPATH="$OPENWEBUI_BACKEND_DIR"
export HOST="$WEBUI_HOST"
export PORT="$WEBUI_PORT"

echo "[INFO] Starting Open WebUI on http://${WEBUI_HOST}:${WEBUI_PORT}"
echo "[INFO] OPENAI_API_BASE_URLS=${OPENAI_API_BASE_URLS}"
echo "[INFO] OFFLINE_MODE=${OFFLINE_MODE}, BYPASS_EMBEDDING_AND_RETRIEVAL=${BYPASS_EMBEDDING_AND_RETRIEVAL}"

exec bash ./start.sh --workers "${UVICORN_WORKERS:-1}"
