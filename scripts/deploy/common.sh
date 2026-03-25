#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_DIR="$ROOT_DIR/infra/env"
LEGACY_ENV_FILE="$ENV_DIR/.env.dev"
HOST_ENV_FILES=("$ENV_DIR/common.env" "$ENV_DIR/host.env" "$ENV_DIR/secrets.env")
COMPOSE_ENV_FILES=("$ENV_DIR/common.env" "$ENV_DIR/compose.env" "$ENV_DIR/secrets.env")
CORE_COMPOSE_FILE="$ROOT_DIR/infra/compose/core.yml"
OBSERVABILITY_COMPOSE_FILE="$ROOT_DIR/infra/compose/observability.yml"

source_env_files() {
  local mode="$1"
  local -a files=()
  if [[ "$mode" == "host" ]]; then
    files=("${HOST_ENV_FILES[@]}")
  else
    files=("${COMPOSE_ENV_FILES[@]}")
  fi

  local all_present="1"
  local file
  for file in "${files[@]}"; do
    if [[ ! -f "$file" ]]; then
      all_present="0"
      break
    fi
  done

  if [[ "$all_present" == "1" ]]; then
    set -a
    for file in "${files[@]}"; do
      # shellcheck disable=SC1090
      source "$file"
    done
    set +a
    return 0
  fi

  if [[ -f "$LEGACY_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$LEGACY_ENV_FILE"
    set +a
    return 0
  fi

  echo "[ERROR] Missing environment files for mode '$mode'." >&2
  echo "[ERROR] Expected ${files[*]} or legacy fallback $LEGACY_ENV_FILE" >&2
  exit 1
}

require_env_vars() {
  local missing=()
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("$name")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    echo "[ERROR] Missing required environment variables: ${missing[*]}" >&2
    return 1
  fi
}

compose_args() {
  printf '%s\n' \
    "-f" "$CORE_COMPOSE_FILE" \
    "-f" "$OBSERVABILITY_COMPOSE_FILE"
}

wait_for_container_health() {
  local container_name="$1"
  local timeout_seconds="${2:-120}"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    local state
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_name" 2>/dev/null || true)"
    if [[ "$state" == "healthy" || "$state" == "running" ]]; then
      return 0
    fi
    sleep 2
  done

  echo "[ERROR] Container did not become healthy in time: $container_name" >&2
  docker inspect "$container_name" >/dev/null 2>&1 && docker inspect "$container_name" >&2 || true
  return 1
}
