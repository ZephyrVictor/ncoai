# Compose Layout

The deployment now uses two compose entrypoints:

- `core.yml`
  - `postgres`
  - `redis`
  - `nginx`
- `observability.yml`
  - `prometheus`
  - `grafana`
  - `loki`
  - `alloy`
  - `node-exporter`
  - `postgres-exporter`
  - `redis-exporter`
  - `blackbox-exporter`

Application services are intentionally **not** containerized in this phase:

- `vLLM`
- `LiteLLM`
- `Chainlit`
- `Admin`

Those run on the host and are managed by `python3 devctl.py up`.

## Environment Sources

Preferred deployment env files:

- `infra/env/common.env`
- `infra/env/compose.env`
- `infra/env/secrets.env`

Legacy fallback:

- `infra/env/.env.dev`

## Common Commands

```bash
bash scripts/deploy/init-infra.sh
bash scripts/deploy/init-stack.sh
bash scripts/deploy/start-services.sh
python3 devctl.py up
python3 devctl.py down
```

## Offline Image List

See:

- `infra/compose/images.manifest.txt`
