# Single-Node Deployment

## Deployment Shape

- Host:
  - `vLLM`
  - `LiteLLM`
  - `Chainlit`
  - `Admin`
- Containers:
  - `PostgreSQL`
  - `Redis`
  - `Nginx`
  - `Prometheus`
  - `Grafana`
  - `Loki`
  - `Promtail`
  - exporters

This layout keeps GPU inference on the host while containerizing stateful infra and edge services.

## Config Files

Prepare these local files from the templates in `infra/env/`:

- `common.env`
- `host.env`
- `compose.env`
- `secrets.env`

Fallback for legacy development remains `infra/env/.env.dev`, but deployment should use the split files and avoid hidden runtime defaults.

To create the split env files from the checked-in examples:

```bash
bash scripts/deploy/prepare-env.sh
```

## Manual Prerequisites

- Host model directory already present and pointed to by `MODEL_PATH`
- Python 3.11, `uv`, `tmux`, `docker`, `nvidia-smi`
- Docker images mirrored or preloaded from `infra/compose/images.manifest.txt`
- If host Python dependencies must be installed offline:
  - prepare a wheelhouse
  - set `UV_PIP_EXTRA_ARGS="--no-index --find-links /path/to/wheels"`
- If Node.js cannot be downloaded on the target host:
  - preinstall Node.js on the host, or
  - keep the committed Chainlit dist assets and set `BUILD_CHAINLIT_FRONTEND=0`
- If Playwright browsers cannot be downloaded on the target host:
  - preinstall them separately, or
  - set `INSTALL_PLAYWRIGHT_BROWSERS=0` and skip browser E2E on that machine

## Initialization Order

Fast path:

```bash
python3 devctl.py init
python3 devctl.py up apps
```

`python3 devctl.py init` runs the three deployment initialization scripts in order:

1. `bash scripts/deploy/init-host.sh`
2. `bash scripts/deploy/init-infra.sh`
3. `bash scripts/deploy/init-db.sh`

Application startup is still a separate step:

1. `python3 devctl.py up apps`

If you are migrating data from an older server, prefer the fine-grained sequence below so you can restore databases and storage between infra startup and `init-db`.

Equivalent fine-grained steps:

1. `bash scripts/deploy/init-host.sh`
2. `bash scripts/deploy/init-infra.sh`
3. `bash scripts/deploy/init-db.sh`
4. `python3 devctl.py up apps`

See `docs/server-migration-init.md` for the full migration checklist, including which files and directories must be copied to a new host.

## `devctl` Responsibilities

- `python3 devctl.py up`
  - `vllm`
  - `litellm`
  - `chainlit`
  - `admin`
- `python3 devctl.py down`
  - host services only
- `python3 devctl.py doctor`
  - prerequisite and health diagnostics

Host service startup is strict:

- `chainlit` and `admin` fail fast if `DATABASE_URL` / `REDIS_URL` are missing
- `litellm` fails fast if `LITELLM_DATABASE_URL` is missing
- host services validate database connectivity and required tables before starting
- startup no longer silently reconstructs DB URLs from partial env vars

Database separation:

- `DATABASE_URL` is the application database (`ncoai_chat`) used by `chainlit` and `admin`
- `LITELLM_DATABASE_URL` is a dedicated LiteLLM database (recommended: `ncoai_litellm`)
- do not point both variables to the same database in production

Initialization stays outside `devctl up/down`:

- infra/database/bootstrap is done by `scripts/deploy/init-*.sh`
- `devctl` no longer starts or stops PostgreSQL/Redis/Nginx/Prometheus/Grafana

## Validation

Run the full deployment validation:

```bash
bash scripts/deploy/validate.sh
```

This runs:

- infra health checks
- metrics entrypoint checks
- Grafana / Loki datasource and dashboard checks
- proxy and websocket checks
- admin frontend E2E
- chat frontend E2E
- disabled feature-flag UI checks
- quota and deletion ledger checks

## HTTPS

The deployment nginx can keep the existing internal HTTP entrypoint on `127.0.0.1:8088` while also serving a public domain on ports `80` and `443`.

Recommended env values:

- `NGINX_SERVER_NAME=ncoai.example.com`
- `NGINX_PUBLIC_HTTP_PORT=80`
- `NGINX_PUBLIC_HTTPS_PORT=443`
- `NGINX_ENABLE_TLS=0`
- `NGINX_SSL_CERT=/etc/letsencrypt/live/ncoai.example.com/fullchain.pem`
- `NGINX_SSL_KEY=/etc/letsencrypt/live/ncoai.example.com/privkey.pem`

Suggested rollout:

1. restart nginx so it serves the public HTTP domain on port `80`
2. request the certificate:

```bash
LETSENCRYPT_EMAIL=you@example.com bash scripts/deploy/request-cert.sh
```

To issue one certificate covering multiple hostnames while keeping a single
canonical `NGINX_SERVER_NAME`, override the requested SAN list explicitly:

```bash
LETSENCRYPT_EMAIL=you@example.com \
LETSENCRYPT_CERT_NAME=ncoai.example.com \
LETSENCRYPT_DOMAINS="ncoai.example.com alias.example.com" \
bash scripts/deploy/request-cert.sh
```

If Docker Hub is slow on the target machine, set `CERTBOT_IMAGE` in `infra/env/compose.env` to a reachable mirror first.

3. set `NGINX_ENABLE_TLS=1`
4. restart nginx again

After that, nginx redirects `http://<domain>` to `https://<domain>` while validation scripts can still use `http://127.0.0.1:8088`.

## FTP Uploads

录像上传改为宿主机上的显式 FTPS 服务，不经过 nginx。

Recommended env values:

- `CAPS_ENABLED=1`
- `CAPS_STORAGE_ROOT=/data/ncoai/caps`
- `CAPS_FTP_HOST=0.0.0.0`
- `CAPS_FTP_PORT=3221`
- `CAPS_FTP_USERNAME=<fixed-account>`
- `CAPS_FTP_PASSWORD=<fixed-password>`
- `CAPS_FTP_ADMIN_USERNAME=<full-access-account>` optional
- `CAPS_FTP_ADMIN_PASSWORD=<full-access-password>` optional
- `CAPS_FTP_PASSIVE_PORT_START=41000`
- `CAPS_FTP_PASSIVE_PORT_END=41099`
- `CAPS_FTP_PUBLIC_HOST=<public-ip-or-domain>` when the host is behind NAT
- `CAPS_FTP_TLS_ENABLED=1`
- `CAPS_FTP_TLS_CERTFILE=/path/to/readable/fullchain.pem`
- `CAPS_FTP_TLS_KEYFILE=/path/to/readable/privkey.pem`
- `CAPS_FTP_TLS_CONTROL_REQUIRED=1`
- `CAPS_FTP_TLS_DATA_REQUIRED=1`

Deployment checklist:

1. set `CAPS_STORAGE_ROOT=/data/ncoai/caps`
2. configure `CAPS_FTP_USERNAME` and `CAPS_FTP_PASSWORD`
3. optionally configure `CAPS_FTP_ADMIN_USERNAME` and `CAPS_FTP_ADMIN_PASSWORD` for a full-access FTP account
4. copy the TLS certificate and private key to a path readable by the host `chainlit` process
5. open the FTPS control port `CAPS_FTP_PORT`
6. open the passive range `CAPS_FTP_PASSIVE_PORT_START` to `CAPS_FTP_PASSIVE_PORT_END`
7. point users at the FTP account they should use; both accounts are chrooted to `/data/ncoai/caps/game-<GZCTF_FIXED_GAME_ID>`, while the optional admin account has full FTP permissions inside that root

Client guidance:

- use `FTP over TLS (explicit)` instead of plain FTP or SFTP
- keep `Passive` mode enabled
- if using a copied or self-signed certificate, trust/import that certificate on the client first

## Offline Image Preparation

Prepare the container images listed in:

- `infra/compose/images.manifest.txt`

If your registry mirror is constrained, preload or mirror those exact images first. The compose defaults and the manifest are now aligned to the current local image set instead of introducing a new pinned image family.

## Container Inventory

Required:

- `postgres:16` -> container `ncoai-postgres` -> port `55432`
- `redis:7` -> container `ncoai-redis` -> port `6379`
- `nginx:alpine` -> container `ncoai-nginx` -> port `8088`
- `prom/prometheus:latest` -> container `ncoai-prometheus` -> port `9090`
- `grafana/grafana:latest` -> container `ncoai-grafana` -> port `3001`
- `grafana/loki:3.1.0` -> container `ncoai-loki` -> port `3101`
- `grafana/promtail:3.1.0` -> container `ncoai-promtail`

Recommended:

- `prom/node-exporter:latest` -> container `ncoai-node-exporter`
- `prometheuscommunity/postgres-exporter:latest` -> container `ncoai-postgres-exporter`
- `oliver006/redis_exporter:latest` -> container `ncoai-redis-exporter`
- `prom/blackbox-exporter:latest` -> container `ncoai-blackbox-exporter`

## Ops Entry Points

- Admin summary: `/admin/ops`
- Grafana dashboards: `/grafana/`
- Host-only metrics:
  - `http://127.0.0.1:${ADMIN_APP_PORT}${ADMIN_APP_ROOT_PATH}/metrics`
  - `http://127.0.0.1:${CHAINLIT_PORT}${CHAINLIT_ROOT_PATH}/metrics`
