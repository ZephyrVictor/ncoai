# Dev Runbook

## Start order

1. `postgres`
2. `vLLM`
3. `LiteLLM`
4. `Chainlit`
5. `nginx` when available

`python3 devctl.py up` starts steps 2 to 4 in tmux. PostgreSQL and nginx remain external services.

## Service commands

### Full local stack

```bash
python3 devctl.py up
python3 devctl.py status
python3 devctl.py attach
```

### Stop local stack

```bash
python3 devctl.py down
```

### Optional edge services

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d postgres
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d nginx
```

## Health checks

### vLLM

```bash
curl -s http://127.0.0.1:12233/health
curl -s http://127.0.0.1:12233/v1/models
```

### LiteLLM

```bash
curl -s http://127.0.0.1:33455/v1/models \
  -H "Authorization: Bearer sk-local-dev"
```

Expected aliases:

1. `qwen35b_no_think`
2. `qwen35b_think`

### Chainlit

```bash
curl -s http://127.0.0.1:3100/chat
```

### nginx

```bash
curl -I http://127.0.0.1:8088/
curl -I http://127.0.0.1:8088/chat
```

Expected behavior:

1. `/` redirects to `/chat`
2. `/chat` proxies to Chainlit

## Acceptance run

```bash
python3 scripts/e2e_dev_acceptance.py
```

The script produces a JSON report and checks:

1. service reachability
2. browser login
3. first chat round-trip
4. thread persistence
5. thread resume
6. LiteLLM spend log linkage

## Common failure points

### nginx image pull fails

Symptom:

`docker compose ... up -d nginx` times out against Docker Hub.

Action:

1. keep using `http://127.0.0.1:3100/chat` for development
2. do not claim nginx validation is complete until the image can actually be started

### Playwright package is present but Chromium is missing

Symptom:

Playwright fails to launch a browser.

Action:

```bash
python -m playwright install chromium
```

### LiteLLM is up but usage table is empty

Action:

1. confirm the browser chat request completed
2. query `http://127.0.0.1:33455/spend/logs`
3. confirm request metadata contains `chainlit_thread_id` and `chainlit_user_identifier`

### Chainlit starts but thread history fails

Action:

1. inspect `python3 devctl.py logs chainlit`
2. confirm `DATABASE_URL` points to `ncoai_chat`
3. confirm the `threads` and `steps` tables exist in PostgreSQL
