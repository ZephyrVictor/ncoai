# Dev Runbook

## One-Click Control

From root directory:

```bash
python3 devctl.py up
python3 devctl.py status
python3 devctl.py attach
python3 devctl.py down
```

## Start order

1. vLLM local process
2. LiteLLM local process
3. Open WebUI local process
4. Optional edge services (Docker or local)

### Start commands

1. vLLM:
```bash
source /home/fjw25/workspace/ncoai/infra/env/.env.dev
source /home/fjw25/workspace/ncoai/.venv/bin/activate
bash /home/fjw25/workspace/ncoai/scripts/vllm/start-qwen35b.sh
```
2. LiteLLM:
```bash
source /home/fjw25/workspace/ncoai/infra/env/.env.dev
bash /home/fjw25/workspace/ncoai/scripts/litellm/start-local.sh
```
3. Open WebUI:
```bash
source /home/fjw25/workspace/ncoai/infra/env/.env.dev
bash /home/fjw25/workspace/ncoai/scripts/openwebui/start-local.sh
```

## Stop order

1. Open WebUI
2. LiteLLM
3. vLLM
4. Edge services

## Quick checks

1. vLLM health:
```bash
curl -s http://127.0.0.1:8000/health
```
2. LiteLLM model list:
```bash
curl -s http://127.0.0.1:4000/v1/models
```
3. Chat test through LiteLLM:
```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen35b","messages":[{"role":"user","content":"hello"}]}'
```

## Fallback strategy

If Docker image pull/network fails, run edge services locally.
Do not containerize vLLM in this phase.
