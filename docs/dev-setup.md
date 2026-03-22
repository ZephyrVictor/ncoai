# Local Development Setup

## 1. Prepare environment

1. Install Python 3.10+ and uv.
2. Install Node.js and pnpm/npm (follow Open WebUI repo docs).
3. Ensure NVIDIA driver and CUDA runtime are available for vLLM.
4. Create one shared root env:

```bash
uv venv /home/fjw25/workspace/ncoai/.venv --python 3.11
```

## 2. Start vLLM (local process)

```bash
source infra/env/.env.dev
source .venv/bin/activate
bash scripts/vllm/start-qwen35b.sh
```

## 3. Start LiteLLM (local source)

```bash
source infra/env/.env.dev
source .venv/bin/activate
bash scripts/litellm/start-local.sh
```

## 4. Start Open WebUI (local source)

```bash
source infra/env/.env.dev
bash scripts/openwebui/start-local.sh
```

Open WebUI will be available on `http://127.0.0.1:8080` by default.

## 5. One command with tmux (recommended)

From project root:

```bash
python3 devctl.py up
```

Useful commands:

```bash
python3 devctl.py status
python3 devctl.py logs vllm
python3 devctl.py logs litellm
python3 devctl.py logs webui
python3 devctl.py attach
python3 devctl.py down
```

## 6. Optional edge services with Docker

```bash
docker compose -f infra/compose/dev-services.yml --env-file infra/env/.env.dev up -d
```

## 7. If Docker network is broken

Start PostgreSQL/Redis/Prometheus/Grafana locally with native services, then keep app chain unchanged.
