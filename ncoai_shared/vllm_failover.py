from __future__ import annotations

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
    primary_endpoint: VLLMEndpoint
    backup_endpoint: VLLMEndpoint | None
    primary_request_timeout_seconds: float
    backup_request_timeout_seconds: float
    lastdance_enabled: bool
    lastdance_endpoint: VLLMEndpoint
    lastdance_target_model: str
    lastdance_request_timeout_seconds: float


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
    lastdance_enabled = parse_env_bool(values.get("thisisthelastdance"), default=False)
    backup_origin = str(values.get("VLLM_BACKUP_ORIGIN") or "").strip()
    if enabled and not backup_origin and not lastdance_enabled:
        raise ValueError("VLLM_FAILOVER_ENABLED=1 requires VLLM_BACKUP_ORIGIN")

    served_model_name = str(values.get("SERVED_MODEL_NAME") or "qwen35b").strip()
    if not served_model_name:
        served_model_name = "qwen35b"

    return LiteLLMFailoverSettings(
        enabled=enabled,
        served_model_name=served_model_name,
        primary_endpoint=build_vllm_endpoint(
            build_local_origin(
                values.get("VLLM_HOST", "127.0.0.1"),
                values.get("VLLM_PORT", "12233"),
            )
        ),
        backup_endpoint=build_vllm_endpoint(backup_origin) if enabled else None,
        primary_request_timeout_seconds=parse_env_float(
            values.get("VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS"),
            default=2.5,
        ),
        backup_request_timeout_seconds=parse_env_float(
            values.get("VLLM_BACKUP_REQUEST_TIMEOUT_SECONDS"),
            default=5.0,
        ),
        lastdance_enabled=lastdance_enabled,
        lastdance_endpoint=build_vllm_endpoint("http://127.0.0.1:12234"),
        lastdance_target_model="glm-4.5-air",
        lastdance_request_timeout_seconds=parse_env_float(
            values.get("CHAINLIT_OPENAI_TIMEOUT"),
            default=300.0,
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


def _backup_model_block(settings: LiteLLMFailoverSettings) -> str:
    return ""


def _lastdance_model_block(settings: LiteLLMFailoverSettings) -> str:
    return ""


def _fallbacks_block(settings: LiteLLMFailoverSettings) -> str:
    return ""


def _primary_extra_body_block(*, enable_thinking: bool, settings: LiteLLMFailoverSettings) -> str:
    if settings.lastdance_enabled:
        return ""
    enabled = "true" if enable_thinking else "false"
    return (
        "      extra_body:\n"
        "        chat_template_kwargs:\n"
        f"          enable_thinking: {enabled}\n"
    )


def build_litellm_template_context(
    settings: LiteLLMFailoverSettings,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = dict(env or {})
    primary_model = f"openai/{settings.served_model_name}"
    primary_api_base = settings.lastdance_endpoint.api_base
    primary_request_timeout_seconds = settings.lastdance_request_timeout_seconds
    backup_model_block = _backup_model_block(settings)
    fallbacks_block = _fallbacks_block(settings)

    if settings.lastdance_enabled:
        primary_model = f"openai/{settings.lastdance_target_model}"
        primary_api_base = settings.lastdance_endpoint.api_base
        primary_request_timeout_seconds = settings.lastdance_request_timeout_seconds
        backup_model_block = ""
        fallbacks_block = ""

    return {
        "primary_model": primary_model,
        "primary_api_base": primary_api_base,
        "primary_request_timeout_seconds": format_timeout(
            primary_request_timeout_seconds
        ),
        "litellm_set_verbose": format_yaml_bool(
            parse_env_bool(values.get("LITELLM_SET_VERBOSE"), default=True)
        ),
        "primary_no_think_extra_body_block": _primary_extra_body_block(
            enable_thinking=False,
            settings=settings,
        ),
        "primary_think_extra_body_block": _primary_extra_body_block(
            enable_thinking=True,
            settings=settings,
        ),
        "backup_model_block": backup_model_block,
        "lastdance_model_block": _lastdance_model_block(settings),
        "fallbacks_block": fallbacks_block,
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

    return PreparedLiteLLMConfig(
        rendered_config=render_litellm_config(template_text, settings, env=env),
        settings=settings,
        warnings=tuple(warnings),
        backup_probe=backup_probe,
    )
