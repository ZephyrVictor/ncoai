# dev-services compose

This compose file is only for edge services in development:

1. PostgreSQL
2. Redis
3. Prometheus
4. Grafana

vLLM is not containerized in this phase.
LiteLLM and Open WebUI are expected to run from local source in `middleware/`.
