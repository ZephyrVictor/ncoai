# Local Development Setup

## Scope

This repository now delivers one chat path only:

`Browser -> Chainlit -> LiteLLM -> vLLM`

The current MVP does not include agent workflows, RAG, MCP, or the old control-plane chat path.

## 1. Prerequisites

1. Python `3.11`
2. `uv`
3. `tmux`
4. NVIDIA driver and CUDA runtime for `vLLM`
5. Docker for optional edge services such as PostgreSQL and nginx

You do not need a system `node` or `pnpm`. `scripts/chainlit/start-local.sh` installs a local Node 20 toolchain under `.local/toolchain/`.

## 2. Create the shared Python environment

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
```

## 3. Load local environment

Use exported env vars, not a plain `source`, because child processes need them.

```bash
set -a
source infra/env/.env.dev
set +a
```

Important defaults in `infra/env/.env.dev`:

1. `vLLM` serves `qwen35b` on `12233`
2. `LiteLLM` listens on `33455`
3. `Chainlit` listens on `3100` with root path `/chat`
4. Default acceptance login is `acceptance / acceptance123`
5. Database target is `ncoai_chat`

## 4. Start the stack

Recommended:

```bash
python3 devctl.py up
```

Available panes:

1. `vllm`
2. `litellm`
3. `chainlit`

Useful commands:

```bash
python3 devctl.py status
python3 devctl.py logs vllm
python3 devctl.py logs litellm
python3 devctl.py logs chainlit
python3 devctl.py attach
python3 devctl.py down
```

## 5. Optional edge services

PostgreSQL is required. nginx is optional for local development but is the intended external entrypoint.

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d postgres
```

If Docker Hub access works, you can also bring up nginx:

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d nginx
```

If nginx cannot be pulled, use direct Chainlit access during development:

`http://127.0.0.1:3100/chat`

## 6. Install browser runtime for acceptance

The Python package is installed automatically by the Chainlit startup script, but the Chromium browser binary still needs a one-time install:

```bash
python -m playwright install chromium
```

## 7. Run the acceptance script

The acceptance script validates:

1. `vLLM` health
2. `LiteLLM` model listing
3. Browser login
4. New chat creation
5. Streaming response
6. Thread persistence and resume
7. LiteLLM usage log presence

```bash
python3 scripts/e2e_dev_acceptance.py
```

By default it tries `http://127.0.0.1:8088/chat` first, then falls back to `http://127.0.0.1:3100/chat` if nginx is unavailable.

## 8. Manual access points

1. Preferred edge URL: `http://127.0.0.1:8088/chat`
2. Direct app URL: `http://127.0.0.1:3100/chat`
3. LiteLLM models: `http://127.0.0.1:33455/v1/models`
4. vLLM models: `http://127.0.0.1:12233/v1/models`
