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
  - exporters

This layout keeps GPU inference on the host while containerizing stateful infra and edge services.

## Config Files

Prepare these local files from the templates in `infra/env/`:

- `common.env`
- `host.env`
- `compose.env`
- `secrets.env`

Fallback for legacy development remains `infra/env/.env.dev`, but deployment should use the split files and avoid hidden runtime defaults.

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

1. `bash scripts/deploy/init-host.sh`
2. `bash scripts/deploy/init-stack.sh`
3. `bash scripts/deploy/start-services.sh`

Equivalent fine-grained steps:

1. `bash scripts/deploy/init-host.sh`
2. `bash scripts/deploy/init-infra.sh`
3. `bash scripts/deploy/init-db.sh`
4. `python3 devctl.py up apps`

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
- proxy and websocket checks
- admin frontend E2E
- chat frontend E2E
- disabled feature-flag UI checks
- quota and deletion ledger checks

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

Recommended:

- `prom/node-exporter:latest` -> container `ncoai-node-exporter`
- `prometheuscommunity/postgres-exporter:latest` -> container `ncoai-postgres-exporter`
- `oliver006/redis_exporter:latest` -> container `ncoai-redis-exporter`
- `prom/blackbox-exporter:latest` -> container `ncoai-blackbox-exporter`
