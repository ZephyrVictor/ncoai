#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/deploy/common.sh"
source_env_files host

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

if ! docker inspect ncoai-postgres >/dev/null 2>&1; then
  echo "[ERROR] PostgreSQL container not found: ncoai-postgres" >&2
  echo "[ERROR] Start infrastructure first or run this on the old server where the stack is active." >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
default_bundle_dir="$ROOT_DIR/.local/migration/ncoai-migration-$timestamp"
bundle_dir="${1:-$default_bundle_dir}"
bundle_dir="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$bundle_dir")"
bundle_name="$(basename "$bundle_dir")"
bundle_parent="$(dirname "$bundle_dir")"
bundle_tarball="${bundle_dir}.tar.gz"

mkdir -p "$bundle_dir/env" "$bundle_dir/db" "$bundle_dir/meta"

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

write_manifest() {
  local app_db_name="$1"
  local litellm_db_name="$2"
  local caps_archive_name="$3"
  local git_revision="$4"
  python3 - "$bundle_dir/meta/manifest.json" "$timestamp" "$app_db_name" "$litellm_db_name" "$caps_archive_name" "$git_revision" <<'PY'
import json
import os
import socket
import sys

manifest_path, timestamp, app_db_name, litellm_db_name, caps_archive_name, git_revision = sys.argv[1:]
payload = {
    "created_at_utc": timestamp,
    "host": socket.gethostname(),
    "git_revision": git_revision,
    "bundle_format": 1,
    "databases": {
        "application": app_db_name,
        "litellm": litellm_db_name,
    },
    "caps_archive": caps_archive_name,
    "env_files": [
        "env/common.env",
        "env/host.env",
        "env/compose.env",
        "env/secrets.env",
    ],
}
with open(manifest_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
PY
}

dump_database() {
  local label="$1"
  local database_name="$2"
  local output_file="$3"
  if [[ -z "$database_name" ]]; then
    echo "[WARN] Skipping $label database backup because the database name is empty"
    return 0
  fi
  echo "[INFO] Dumping database '$database_name' -> $output_file"
  docker exec \
    -e PGPASSWORD="${POSTGRES_PASSWORD}" \
    ncoai-postgres \
    pg_dump \
    -U "${POSTGRES_USER}" \
    -d "$database_name" \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    | gzip -c > "$output_file"
}

copy_env_file() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    cp "$src" "$dst"
  fi
}

copy_optional_file() {
  local env_name="$1"
  local src_path="${!env_name:-}"
  if [[ -z "$src_path" ]]; then
    return 0
  fi
  if [[ ! -r "$src_path" ]]; then
    echo "[WARN] Cannot read $env_name from $src_path; skipping"
    return 0
  fi

  local rel_path="certs/${src_path#/}"
  local dst_path="$bundle_dir/$rel_path"
  mkdir -p "$(dirname "$dst_path")"
  cp "$src_path" "$dst_path"
  printf '%s\t%s\t%s\n' "$env_name" "$src_path" "$rel_path" >> "$bundle_dir/meta/certificates.tsv"
}

copy_env_file "$ROOT_DIR/infra/env/common.env" "$bundle_dir/env/common.env"
copy_env_file "$ROOT_DIR/infra/env/host.env" "$bundle_dir/env/host.env"
copy_env_file "$ROOT_DIR/infra/env/compose.env" "$bundle_dir/env/compose.env"
copy_env_file "$ROOT_DIR/infra/env/secrets.env" "$bundle_dir/env/secrets.env"

app_db_name="$(database_name_from_url "${DATABASE_URL:-}")"
litellm_db_name="$(database_name_from_url "${LITELLM_DATABASE_URL:-}")"

dump_database "application" "$app_db_name" "$bundle_dir/db/application.sql.gz"
dump_database "litellm" "$litellm_db_name" "$bundle_dir/db/litellm.sql.gz"

caps_archive_name=""
if [[ "${CAPS_ENABLED:-0}" == "1" && -n "${CAPS_STORAGE_ROOT:-}" ]]; then
  if [[ -d "$CAPS_STORAGE_ROOT" ]]; then
    caps_archive_name="caps-storage.tar.gz"
    echo "[INFO] Archiving CAPS storage from $CAPS_STORAGE_ROOT"
    tar -C "$(dirname "$CAPS_STORAGE_ROOT")" -czf "$bundle_dir/$caps_archive_name" "$(basename "$CAPS_STORAGE_ROOT")"
  else
    echo "[WARN] CAPS storage root does not exist: $CAPS_STORAGE_ROOT"
  fi
fi

: > "$bundle_dir/meta/certificates.tsv"
copy_optional_file "NGINX_SSL_CERT"
copy_optional_file "NGINX_SSL_KEY"
copy_optional_file "CAPS_FTP_TLS_CERTFILE"
copy_optional_file "CAPS_FTP_TLS_KEYFILE"

git_revision="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
write_manifest "$app_db_name" "$litellm_db_name" "$caps_archive_name" "$git_revision"

mkdir -p "$bundle_parent"
tar -C "$bundle_parent" -czf "$bundle_tarball" "$bundle_name"

echo "[INFO] Migration bundle directory: $bundle_dir"
echo "[INFO] Migration bundle archive: $bundle_tarball"
echo "[WARN] The migration bundle contains secrets.env and may contain TLS/FTPS private keys; handle it as sensitive data."
