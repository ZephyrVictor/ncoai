#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files compose

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] Required command not found: $cmd" >&2
    exit 1
  fi
}

need_cmd docker

echo "[INFO] Starting containerized infrastructure"
if ! docker compose $(compose_args) up -d; then
  echo "[ERROR] Failed to start infrastructure containers." >&2
  echo "[ERROR] If the target server cannot pull from Docker Hub, preload the images listed in infra/compose/images.manifest.txt and set them in infra/env/compose.env." >&2
  exit 1
fi

for container in \
  ncoai-postgres \
  ncoai-redis \
  ncoai-nginx \
  ncoai-prometheus \
  ncoai-grafana \
  ncoai-loki \
  ncoai-promtail \
  ncoai-node-exporter \
  ncoai-postgres-exporter \
  ncoai-redis-exporter \
  ncoai-blackbox-exporter
do
  wait_for_container_health "$container" 180
done

echo "[INFO] Infrastructure initialization completed"
