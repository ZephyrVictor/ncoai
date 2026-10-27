#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS_FILE="$ROOT_DIR/infra/env/secrets.env"

if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "[ERROR] Run scripts/deploy/prepare-env.sh first." >&2
  exit 1
fi

if ! grep -q 'replace-me' "$SECRETS_FILE"; then
  echo "[INFO] Existing deployment secrets kept"
  exit 0
fi

for placeholder in postgres litellm chainlit-auth chat-password admin-password admin-session; do
  if ! grep -q "replace-me-$placeholder" "$SECRETS_FILE"; then
    echo "[ERROR] Refusing to replace partially configured secrets.env" >&2
    exit 1
  fi
done

export NCOAI_PG_SECRET="$(openssl rand -hex 24)"
export NCOAI_LITELLM_SECRET="$(openssl rand -hex 24)"
export NCOAI_CHAINLIT_SECRET="$(openssl rand -hex 32)"
export NCOAI_CHAT_SECRET="$(openssl rand -hex 24)"
export NCOAI_ADMIN_SECRET="$(openssl rand -hex 24)"
export NCOAI_SESSION_SECRET="$(openssl rand -hex 32)"

perl -pi -e '
  s/replace-me-postgres/$ENV{NCOAI_PG_SECRET}/g;
  s/replace-me-litellm/$ENV{NCOAI_LITELLM_SECRET}/g;
  s/replace-me-chainlit-auth/$ENV{NCOAI_CHAINLIT_SECRET}/g;
  s/replace-me-chat-password/$ENV{NCOAI_CHAT_SECRET}/g;
  s/replace-me-admin-password/$ENV{NCOAI_ADMIN_SECRET}/g;
  s/replace-me-admin-session/$ENV{NCOAI_SESSION_SECRET}/g;
' "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"
unset NCOAI_PG_SECRET NCOAI_LITELLM_SECRET NCOAI_CHAINLIT_SECRET
unset NCOAI_CHAT_SECRET NCOAI_ADMIN_SECRET NCOAI_SESSION_SECRET

if grep -q 'replace-me' "$SECRETS_FILE"; then
  echo "[ERROR] Unreplaced placeholder in secrets.env" >&2
  exit 1
fi
echo "[INFO] Generated deployment secrets in infra/env/secrets.env"
