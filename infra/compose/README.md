# Compose Files

These compose files only manage supporting edge services. The chat application itself runs as local processes.

## Managed by compose

1. `postgres`
2. `redis`
3. `prometheus`
4. `grafana`
5. `nginx`

## Not managed by compose in this MVP

1. `vLLM`
2. `LiteLLM`
3. `Chainlit`

Those three are started with local scripts and `python3 devctl.py up`.

## Primary development compose file

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d postgres
```

If Docker Hub access is healthy, nginx can be started as well:

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d nginx
```

## External routes

1. `http://127.0.0.1:8088/` -> redirects to `/chat`
2. `http://127.0.0.1:8088/chat` -> proxies to Chainlit on `3100`
