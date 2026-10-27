# Middleware

LiteLLM is installed from PyPI into the project's `.venv-litellm` by `scripts/deploy/init-host.sh`.

LiteLLM is used as the single OpenAI-compatible gateway between Chainlit and vLLM. It is responsible for:

1. exposing `qwen35b_no_think`
2. optionally exposing `qwen35b_think`
3. writing token and usage data to PostgreSQL
