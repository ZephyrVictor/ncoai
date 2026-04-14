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
need_cmd curl

email="${LETSENCRYPT_EMAIL:-${1:-}}"
certbot_image="${CERTBOT_IMAGE:-docker.m.daocloud.io/certbot/certbot:latest}"
server_name="${NGINX_SERVER_NAME:-_}"
domain_source="${LETSENCRYPT_DOMAINS:-$server_name}"
cert_name="${LETSENCRYPT_CERT_NAME:-}"
acme_webroot="${NGINX_ACME_WEBROOT:-/tmp/ncoai-certbot}"

if [[ -z "$email" ]]; then
  echo "Usage: LETSENCRYPT_EMAIL=you@example.com bash scripts/deploy/request-cert.sh" >&2
  exit 1
fi

if [[ "$server_name" == "_" ]]; then
  echo "[ERROR] NGINX_SERVER_NAME is still '_'." >&2
  echo "[ERROR] Set a real canonical domain in infra/env/common.env first." >&2
  exit 1
fi

if ! curl -fsSI "http://127.0.0.1:${NGINX_PUBLIC_HTTP_PORT:-80}/" >/dev/null; then
  echo "[ERROR] Public HTTP entrypoint is not reachable on port ${NGINX_PUBLIC_HTTP_PORT:-80}." >&2
  echo "[ERROR] Restart nginx first so it starts listening on the public HTTP port." >&2
  exit 1
fi

mkdir -p "$acme_webroot"

read -r -a domains <<<"$domain_source"
domain_args=()
for domain in "${domains[@]}"; do
  [[ -n "$domain" ]] || continue
  domain_args+=("-d" "$domain")
done

if (( ${#domain_args[@]} == 0 )); then
  echo "[ERROR] No usable domains found in LETSENCRYPT_DOMAINS or NGINX_SERVER_NAME." >&2
  exit 1
fi

cert_name_args=()
if [[ -n "$cert_name" ]]; then
  cert_name_args=(--cert-name "$cert_name" --expand)
fi

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v "$acme_webroot:/var/www/certbot" \
  "$certbot_image" certonly \
  --webroot \
  -w /var/www/certbot \
  --email "$email" \
  --agree-tos \
  --no-eff-email \
  --non-interactive \
  "${cert_name_args[@]}" \
  "${domain_args[@]}"

echo "[INFO] Certificate request finished."
echo "[INFO] If issuance succeeded, set NGINX_ENABLE_TLS=1 and restart nginx."
