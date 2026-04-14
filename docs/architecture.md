# NCOAI Architecture

## Runtime Topology

The project runs as a single-node mixed deployment:

- Host services:
  - `vLLM`
  - `LiteLLM`
  - `Chainlit`
  - `Admin`
- Container services:
  - `PostgreSQL`
  - `Redis`
  - `Nginx`
  - `Prometheus`
  - `Grafana`
  - `Loki`
  - `Promtail`
  - `node-exporter`
  - `postgres-exporter`
  - `redis-exporter`
  - `blackbox-exporter`

Request flow:

1. Browser -> `Nginx`
2. `Nginx` -> `Chainlit` or `Admin`
3. `Chainlit` -> `LiteLLM`
4. `LiteLLM` -> `vLLM`
5. `Chainlit` / `Admin` / `LiteLLM` -> `PostgreSQL` / `Redis`
6. `Prometheus` / `Loki` / `Grafana` observe host services and containers

## Core Application Boundaries

### `apps/chainlit`

- Password login against `managed_users`
- Thread list / resume / rename / delete
- Streaming chat
- Token quota display and blocking
- LiteLLM-backed model routing

### `apps/admin`

- Admin login
- Managed user CRUD
- CSV import preview + confirm
- Single global quota policy management
- Read-only session max token display
- Ops summary at `/admin/ops`
- Prometheus-backed summary/alerts APIs
- Loki-backed log preview APIs

### `ncoai_shared`

- Shared schema definitions
- Control-plane repository
- Redis quota cache helpers
- Security helpers
- Deployment/bootstrap helpers

## Data Ownership

### Chat / Chainlit tables

- `users`
- `threads`
- `steps`
- `elements`
- `feedbacks`

### Control-plane tables

- `admin_accounts`
- `managed_users`
- `quota_policies`
- `user_import_jobs`
- `user_import_job_rows`

### Quota tables

- `quota_usage_ledger`
- `quota_thread_summary`
- `quota_user_summary`

### LiteLLM tables

LiteLLM writes its own operational tables into the same PostgreSQL instance. They are treated as middleware-owned tables and are not part of the product control-plane schema.

## Quota Model

- Session limit: single thread cumulative token usage, default `2048`
- User limit: cumulative settled assistant usage inside the active global quota window
- `Redis` is the hot-path reservation/cache layer
- `PostgreSQL` is the final correctness ledger
- Deleting a thread does not refund token usage:
  - chat history may disappear from `threads`
  - quota remains anchored to `quota_usage_ledger`

## Deployment Roles

- `devctl + tmux` manages host processes
- `docker compose` manages infrastructure containers
- `Grafana` is the main ops dashboard
- `Promtail` collects both compose logs and `.local/devctl/logs/*.log`
- Initialization is independent from runtime startup:
  - `init-host`
  - `init-infra`
  - `init-db`

Runtime services should not install dependencies, create databases, or bootstrap default chat users.
