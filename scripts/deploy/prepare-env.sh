#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_DIR="$ROOT_DIR/infra/env"

declare -a created_files=()
declare -a existing_files=()

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [[ -f "$dst" ]]; then
    existing_files+=("$(basename "$dst")")
    return 0
  fi
  cp "$src" "$dst"
  created_files+=("$(basename "$dst")")
}

copy_if_missing "$ENV_DIR/common.env.example" "$ENV_DIR/common.env"
copy_if_missing "$ENV_DIR/host.env.example" "$ENV_DIR/host.env"
copy_if_missing "$ENV_DIR/compose.env.example" "$ENV_DIR/compose.env"
copy_if_missing "$ENV_DIR/secrets.env.example" "$ENV_DIR/secrets.env"

echo "[INFO] Environment file preparation completed"
if (( ${#created_files[@]} > 0 )); then
  echo "[INFO] Created: ${created_files[*]}"
fi
if (( ${#existing_files[@]} > 0 )); then
  echo "[INFO] Kept existing: ${existing_files[*]}"
fi

if grep -R -n --include='*.env' 'replace-me' "$ENV_DIR" >/dev/null 2>&1; then
  echo "[WARN] Placeholder secrets remain in infra/env/*.env"
fi

if grep -R -n --include='*.env' 'ncoai.example.com' "$ENV_DIR" >/dev/null 2>&1; then
  echo "[WARN] Placeholder domain values remain in infra/env/*.env"
fi

if grep -R -n --include='host.env' '/srv/ncoai/models/' "$ENV_DIR" >/dev/null 2>&1; then
  echo "[WARN] MODEL_PATH still points to the example path in host.env"
fi

cat <<'EOF'
[INFO] Next steps:
[INFO]   1. Edit infra/env/common.env
[INFO]   2. Edit infra/env/host.env
[INFO]   3. Edit infra/env/compose.env
[INFO]   4. Edit infra/env/secrets.env
[INFO]   5. Run python3 devctl.py init
EOF
