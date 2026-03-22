# Development Architecture (Current Phase)

## Principles

1. Source-first local development.
2. vLLM runs on host machine only.
3. LiteLLM and Open WebUI are checked out as local source repositories.
4. Edge services can run in Docker, with local-install fallback.

## Runtime chain

Open WebUI -> LiteLLM -> vLLM

## Container policy

1. Do not containerize vLLM in this phase.
2. Edge services are optional in Docker.
3. If Docker networking is unstable, switch edge services to local mode.
