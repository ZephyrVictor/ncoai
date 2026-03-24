# Development Architecture (Current Phase)

## Principles

1. Source-first local development.
2. vLLM runs on host machine only.
3. LiteLLM is checked out as local source repository.
4. Current runtime is reduced to the minimum inference chain first.
5. Edge services can run in Docker, with local-install fallback.

## Runtime chain

LiteLLM -> vLLM

Chat UI will be connected in a later step, directly against LiteLLM first.

## Container policy

1. Do not containerize vLLM in this phase.
2. LiteLLM runs locally from source.
3. Edge services are optional in Docker.
4. If Docker networking is unstable, switch edge services to local mode.
