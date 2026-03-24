# Middleware Source Repositories

This folder hosts middleware source trees that are patched locally for the MVP runtime.

## Current contents

1. `litellm`

LiteLLM is used as the single OpenAI-compatible gateway between Chainlit and vLLM. It is responsible for:

1. exposing `qwen35b_no_think`
2. optionally exposing `qwen35b_think`
3. writing token and usage data to PostgreSQL
