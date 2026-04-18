#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/deploy/restore-state.sh <bundle-dir-or-tar.gz>" >&2
  exit 1
fi

bundle_input="$1"

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] Required command not found: $cmd" >&2
    exit 1
  fi
}

need_cmd docker
need_cmd gzip
need_cmd python3
need_cmd tar

temp_dir=""
cleanup() {
  if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then
    rm -rf "$temp_dir"
  fi
}
trap cleanup EXIT

resolve_bundle_dir() {
  local input="$1"
  if [[ -d "$input" ]]; then
    python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$input"
    return 0
  fi
  if [[ -f "$input" && "$input" == *.tar.gz ]]; then
    temp_dir="$(mktemp -d)"
    tar -C "$temp_dir" -xzf "$input"
    local first_entry
    first_entry="$(find "$temp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    if [[ -z "$first_entry" ]]; then
      echo "[ERROR] Failed to find extracted bundle directory in $input" >&2
      exit 1
    fi
    python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$first_entry"
    return 0
  fi
  echo "[ERROR] Bundle input does not exist or is not supported: $input" >&2
  exit 1
}

backup_and_copy_env_file() {
  local src="$1"
  local dst="$2"
  local backup_suffix="$3"
  if [[ ! -f "$src" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ -f "$dst" ]]; then
    cp "$dst" "${dst}.${backup_suffix}.bak"
  fi
  cp "$src" "$dst"
}

database_name_from_url() {
  python3 - "$1" <<'PY'
from urllib.parse import urlparse
import sys

url = sys.argv[1].strip()
if not url:
    print("")
    raise SystemExit(0)
parsed = urlparse(url)
path = parsed.path.lstrip("/")
print(path.split("/", 1)[0] if path else "")
PY
}

ensure_database_exists() {
  local database_name="$1"
  if [[ -z "$database_name" ]]; then
    return 0
  fi
  local exists
  exists="$(
    docker exec \
      -e PGPASSWORD="${POSTGRES_PASSWORD}" \
      ncoai-postgres \
      psql \
      -U "${POSTGRES_USER}" \
      -d postgres \
      -tAc "SELECT 1 FROM pg_database WHERE datname = '${database_name}'"
  )"
  if [[ "$exists" == "1" ]]; then
    return 0
  fi
  echo "[INFO] Creating database '$database_name'"
  docker exec \
    -e PGPASSWORD="${POSTGRES_PASSWORD}" \
    ncoai-postgres \
    createdb \
    -U "${POSTGRES_USER}" \
    "$database_name"
}

restore_database_dump() {
  local label="$1"
  local database_name="$2"
  local dump_file="$3"
  if [[ ! -f "$dump_file" ]]; then
    echo "[INFO] No $label database dump found; skipping"
    return 0
  fi
  if [[ -z "$database_name" ]]; then
    echo "[WARN] Cannot restore $label database because its target name is empty"
    return 0
  fi
  ensure_database_exists "$database_name"
  echo "[INFO] Restoring $label database into '$database_name'"
  gzip -dc "$dump_file" | docker exec \
    -i \
    -e PGPASSWORD="${POSTGRES_PASSWORD}" \
    ncoai-postgres \
    psql \
    -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" \
    -d "$database_name"
}

restore_certificates() {
  local bundle_dir="$1"
  local cert_manifest="$bundle_dir/meta/certificates.tsv"
  if [[ ! -f "$cert_manifest" ]]; then
    return 0
  fi

  while IFS=$'\t' read -r env_name destination rel_path; do
    [[ -n "$env_name" ]] || continue
    local src="$bundle_dir/$rel_path"
    if [[ ! -f "$src" ]]; then
      echo "[WARN] Missing bundled certificate payload for $env_name: $src"
      continue
    fi
    if mkdir -p "$(dirname "$destination")" 2>/dev/null && cp "$src" "$destination" 2>/dev/null; then
      echo "[INFO] Restored $env_name -> $destination"
    else
      echo "[WARN] Could not restore $env_name to $destination; copy it manually"
    fi
  done < "$cert_manifest"
}

bundle_dir="$(resolve_bundle_dir "$bundle_input")"
restore_stamp="$(date -u +%Y%m%dT%H%M%SZ)"

backup_and_copy_env_file "$bundle_dir/env/common.env" "$ROOT_DIR/infra/env/common.env" "$restore_stamp"
backup_and_copy_env_file "$bundle_dir/env/host.env" "$ROOT_DIR/infra/env/host.env" "$restore_stamp"
backup_and_copy_env_file "$bundle_dir/env/compose.env" "$ROOT_DIR/infra/env/compose.env" "$restore_stamp"
backup_and_copy_env_file "$bundle_dir/env/secrets.env" "$ROOT_DIR/infra/env/secrets.env" "$restore_stamp"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

if ! docker inspect ncoai-postgres >/dev/null 2>&1; then
  echo "[ERROR] PostgreSQL container not found: ncoai-postgres" >&2
  echo "[ERROR] Run bash scripts/deploy/init-infra.sh on the new server before restoring data." >&2
  exit 1
fi

app_db_name="$(database_name_from_url "${DATABASE_URL:-}")"
litellm_db_name="$(database_name_from_url "${LITELLM_DATABASE_URL:-}")"

restore_database_dump "application" "$app_db_name" "$bundle_dir/db/application.sql.gz"
restore_database_dump "litellm" "$litellm_db_name" "$bundle_dir/db/litellm.sql.gz"

if [[ -f "$bundle_dir/caps-storage.tar.gz" && -n "${CAPS_STORAGE_ROOT:-}" ]]; then
  echo "[INFO] Restoring CAPS storage to $CAPS_STORAGE_ROOT"
  mkdir -p "$(dirname "$CAPS_STORAGE_ROOT")"
  tar -C "$(dirname "$CAPS_STORAGE_ROOT")" -xzf "$bundle_dir/caps-storage.tar.gz"
fi

restore_certificates "$bundle_dir"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[INFO] Running post-restore database/cache synchronization"
  bash "$ROOT_DIR/scripts/deploy/init-db.sh"
else
  echo "[WARN] Root virtualenv is missing; skipped post-restore init-db"
  echo "[WARN] Run bash scripts/deploy/init-host.sh && bash scripts/deploy/init-db.sh before starting services"
fi

echo "[INFO] Restore completed from $bundle_dir"
