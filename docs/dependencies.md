# Dependencies

## Runtime chain

1. `vLLM` serves the single underlying model `qwen35b`
2. `LiteLLM` exposes OpenAI-compatible aliases:
   - `qwen35b_no_think`
   - `qwen35b_think`
3. `Chainlit` provides the chat UI and thread persistence
4. `PostgreSQL` stores Chainlit threads and LiteLLM usage data
5. `nginx` is the intended public edge entrypoint for `/chat`

## Required local tooling

1. Python `3.11`
2. `uv`
3. `tmux`
4. Docker for optional edge services
5. NVIDIA driver and CUDA runtime for `vLLM`

## Automatically managed tooling

1. Node `24.x` is installed locally under `.local/toolchain/`
2. `pnpm 9` is enabled through `corepack`
3. Chainlit backend runtime is installed into `.venv`
4. LiteLLM proxy runtime is installed into `.venv-litellm` so its MCP dependency can differ from Chainlit's

## Python packages required for MVP validation

1. `chainlit`
2. `openai`
3. `requests`
4. `playwright`

The Chainlit startup script installs these when missing.

## Browser runtime

Playwright also needs the Chromium browser binary:

```bash
python -m playwright install chromium
```

## Optional edge services

1. `nginx`
2. `redis`
3. `prometheus`
4. `grafana`

Only PostgreSQL is required for the chat MVP. Redis, Prometheus, and Grafana are not on the critical path for chat acceptance.
