from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from string import Template
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serialize_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_env_float(value: str | None, *, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid float value: {value}") from exc


@dataclass(frozen=True, slots=True)
class VLLMEndpoint:
    origin: str
    api_base: str
    health_url: str
    models_url: str


@dataclass(frozen=True, slots=True)
class LiteLLMFailoverSettings:
    enabled: bool
    served_model_name: str
    default_model_alias: str
    thinking_model_alias: str
    thinking_enabled: bool
    primary_endpoint: VLLMEndpoint
    backup_endpoint: VLLMEndpoint | None
    primary_request_timeout_seconds: float
    backup_request_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PreparedLiteLLMConfig:
    rendered_config: str
    settings: LiteLLMFailoverSettings
    warnings: tuple[str, ...]
    backup_probe: dict[str, Any] | None


def build_local_origin(host: str, port: str | int) -> str:
    resolved_host = str(host or "").strip() or "127.0.0.1"
    resolved_port = str(port or "").strip() or "12233"
    return normalize_origin(f"http://{resolved_host}:{resolved_port}")


def normalize_origin(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("origin is required")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid origin: {value}")

    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    elif path.endswith("/v1"):
        path = path[:-3]

    return f"{parsed.scheme}://{parsed.netloc}{path}"


def build_vllm_endpoint(origin: str) -> VLLMEndpoint:
    normalized = normalize_origin(origin)
    return VLLMEndpoint(
        origin=normalized,
        api_base=f"{normalized}/v1",
        health_url=f"{normalized}/health",
        models_url=f"{normalized}/v1/models",
    )


def build_litellm_failover_settings(
    env: Mapping[str, str] | None = None,
) -> LiteLLMFailoverSettings:
    values = dict(env or {})
    enabled = parse_env_bool(values.get("VLLM_FAILOVER_ENABLED"), default=False)
    backup_origin = str(values.get("VLLM_BACKUP_ORIGIN") or "").strip()
    if enabled and not backup_origin:
        raise ValueError("VLLM_FAILOVER_ENABLED=1 requires VLLM_BACKUP_ORIGIN")

    served_model_name = str(values.get("SERVED_MODEL_NAME") or "qwen35b").strip()
    if not served_model_name:
        served_model_name = "qwen35b"

    default_alias = str(values.get("CHAINLIT_DEFAULT_MODEL") or "qwen35b_no_think").strip()
    thinking_alias = str(values.get("CHAINLIT_THINKING_MODEL") or "qwen35b_think").strip()
    thinking_enabled = parse_env_bool(values.get("CHAINLIT_ENABLE_THINKING"), default=True)
    if thinking_enabled and default_alias == thinking_alias:
        raise ValueError("CHAINLIT_DEFAULT_MODEL and CHAINLIT_THINKING_MODEL must differ")

    return LiteLLMFailoverSettings(
        enabled=enabled,
        served_model_name=served_model_name,
        default_model_alias=default_alias,
        thinking_model_alias=thinking_alias,
        thinking_enabled=thinking_enabled,
        primary_endpoint=build_vllm_endpoint(
            build_local_origin(
                values.get("VLLM_HOST", "127.0.0.1"),
                values.get("VLLM_PORT", "12233"),
            )
        ),
        backup_endpoint=build_vllm_endpoint(backup_origin) if enabled else None,
        primary_request_timeout_seconds=parse_env_float(
            values.get("VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS"),
            default=180.0,
        ),
        backup_request_timeout_seconds=parse_env_float(
            values.get("VLLM_BACKUP_REQUEST_TIMEOUT_SECONDS"),
            default=180.0,
        ),
    )


def _request_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            return {
                "status": int(response.status),
                "body": body,
            }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}".strip()) from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def probe_vllm_endpoint(
    endpoint: VLLMEndpoint,
    *,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    checked_at = utcnow()
    health_ok = False
    models_ok = False
    errors: list[str] = []

    try:
        response = _request_json(endpoint.health_url, timeout_seconds=timeout_seconds)
        health_ok = 200 <= int(response["status"]) < 300
        if not health_ok:
            errors.append(f"health check returned {response['status']}")
    except Exception as exc:
        errors.append(f"health check failed: {exc}")

    try:
        response = _request_json(endpoint.models_url, timeout_seconds=timeout_seconds)
        models_ok = 200 <= int(response["status"]) < 300
        if not models_ok:
            errors.append(f"models check returned {response['status']}")
    except Exception as exc:
        errors.append(f"models check failed: {exc}")

    return {
        "origin": endpoint.origin,
        "api_base": endpoint.api_base,
        "health_url": endpoint.health_url,
        "models_url": endpoint.models_url,
        "checked_at": serialize_datetime(checked_at),
        "health_ok": health_ok,
        "models_ok": models_ok,
        "healthy": health_ok and models_ok,
        "error": "; ".join(errors) if errors else "",
    }


def format_timeout(value: float) -> str:
    return f"{value:g}"


def format_yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def _model_block(
    settings: LiteLLMFailoverSettings,
    *,
    api_base: str,
    timeout_seconds: float,
    suffix: str = "",
) -> str:
    def deployment(alias: str, enable_thinking: bool | None) -> str:
        block = (
            f"  - model_name: {json.dumps(alias + suffix)}\n"
            "    litellm_params:\n"
            f"      model: {json.dumps('openai/' + settings.served_model_name)}\n"
            f"      api_base: {json.dumps(api_base)}\n"
            "      api_key: dummy\n"
            f"      request_timeout: {format_timeout(timeout_seconds)}\n"
        )
        if enable_thinking is not None:
            block += (
                "      extra_body:\n"
                "        chat_template_kwargs:\n"
                f"          enable_thinking: {format_yaml_bool(enable_thinking)}\n"
            )
        return block

    block = deployment(
        settings.default_model_alias,
        False if settings.thinking_enabled else None,
    )
    if settings.thinking_enabled:
        block += deployment(settings.thinking_model_alias, True)
    return block


def _backup_model_block(settings: LiteLLMFailoverSettings) -> str:
    if settings.backup_endpoint is None:
        return ""
    return _model_block(
        settings,
        api_base=settings.backup_endpoint.api_base,
        timeout_seconds=settings.backup_request_timeout_seconds,
        suffix="_backup",
    )


def _fallbacks_block(settings: LiteLLMFailoverSettings) -> str:
    if settings.backup_endpoint is None:
        return ""
    block = (
        "  fallbacks:\n"
        f"    - {json.dumps(settings.default_model_alias)}:\n"
        f"        - {json.dumps(settings.default_model_alias + '_backup')}\n"
    )
    if settings.thinking_enabled:
        block += (
            f"    - {json.dumps(settings.thinking_model_alias)}:\n"
            f"        - {json.dumps(settings.thinking_model_alias + '_backup')}\n"
        )
    return block


def build_litellm_template_context(
    settings: LiteLLMFailoverSettings,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = dict(env or {})
    return {
        "primary_model_block": _model_block(
            settings,
            api_base=settings.primary_endpoint.api_base,
            timeout_seconds=settings.primary_request_timeout_seconds,
        ),
        "litellm_set_verbose": format_yaml_bool(
            parse_env_bool(values.get("LITELLM_SET_VERBOSE"), default=False)
        ),
        "backup_model_block": _backup_model_block(settings),
        "fallbacks_block": _fallbacks_block(settings),
    }


def render_litellm_config(
    template_text: str,
    settings: LiteLLMFailoverSettings,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    return Template(template_text).substitute(
        build_litellm_template_context(settings, env=env)
    )


def prepare_litellm_config(
    template_text: str,
    *,
    env: Mapping[str, str] | None = None,
    probe_timeout_seconds: float = 3.0,
) -> PreparedLiteLLMConfig:
    settings = build_litellm_failover_settings(env)
    warnings: list[str] = []
    backup_probe: dict[str, Any] | None = None
    if settings.backup_endpoint is not None:
        backup_probe = probe_vllm_endpoint(
            settings.backup_endpoint,
            timeout_seconds=probe_timeout_seconds,
        )
        if not backup_probe["healthy"]:
            warnings.append(
                "Backup vLLM endpoint is currently unreachable: "
                f"{backup_probe['origin']} ({backup_probe['error'] or 'probe failed'})"
            )

    return PreparedLiteLLMConfig(
        rendered_config=render_litellm_config(template_text, settings, env=env),
        settings=settings,
        warnings=tuple(warnings),
        backup_probe=backup_probe,
    )
