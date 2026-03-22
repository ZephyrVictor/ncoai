# Dependencies

## Core local processes

1. vLLM: run as local process only (no container).
2. LiteLLM: run from local source checkout.
3. Open WebUI: run from local source checkout.

## Edge services (preferred with container, fallback to local)

1. PostgreSQL
2. Redis
3. Prometheus
4. Grafana

## Package managers

1. Python services: uv
2. Open WebUI frontend/backend dependencies: follow upstream package manager in repository.

## Fallback rule

If Docker image/network is unstable, edge services can be installed and started locally.
Core chat chain must still be Open WebUI -> LiteLLM -> vLLM.
