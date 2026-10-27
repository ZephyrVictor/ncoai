# Dependency upgrade audit (2026-10-26)

## Updated in this repository

- Host initialization now uses Node 24 LTS and upgrades project Python packages.
- LiteLLM proxy 1.106.4 is installed into `.venv-litellm`; its MCP 2.x dependency conflicts with vendored Chainlit's MCP 1.x requirement in `.venv`.
- The app environment resolves OpenAI SDK 3.37.0, Redis client 8.1.0, cryptography 50.0.2, pyOpenSSL 26.4.0, Playwright 1.64.1, FastAPI 0.162.0, SQLAlchemy 2.1.6, and uvicorn 0.54.0. LiteLLM uses OpenAI SDK 2.54.0 in its separate environment.
- Vendored Chainlit was upgraded from 2.10.0 to 2.12.0 with the local socket, session, quota, and frontend changes ported. Frontend packages and the pnpm lock were updated to the newest releases allowed by their current major-version ranges.
- Compose defaults now use PostgreSQL 19 and Redis 8. PostgreSQL 19 mounts its volume at `/var/lib/postgresql`.

## Pending migrations

- vLLM upstream is 0.32.0. The ncoai deployment has no dedicated vLLM environment yet, and the server GPUs are occupied by other workloads. Install and benchmark it in a separate environment before switching the model service.
- An existing PostgreSQL 16 volume cannot be reused directly with 19; restore a database dump or run `pg_upgrade` before starting the new container. Docker Hub was unreachable during this audit, so the new database images were not pulled or smoke tested.
- Loki was advanced to 3.7.9 and Promtail was replaced with Grafana Alloy 1.21.2. Their images and the log pipeline have not been smoke tested because Docker Hub was unreachable.
- React 19, Vite 8, TypeScript 7, Vitest 5, and pnpm 12 are newer major releases than the vendored Chainlit frontend uses. These need a separate frontend major-version migration and compatibility testing.
- bcrypt remains on major 4 because major 5 rejects passwords longer than 72 bytes; existing hashes and password handling need a compatible migration.

The remote ncoai instance is not deployed yet: its project virtual environments are installed, but there are no ncoai containers, deployment environment files, or running project services. No shared server packages or running GPU jobs were changed.
