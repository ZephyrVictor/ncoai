from __future__ import annotations

from time import monotonic
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


def normalize_metric_label(value: str | None, *, fallback: str = "unknown") -> str:
    if not value:
        return fallback
    return (
        value.strip()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace(":", "_")
        .replace(".", "_")
        .lower()
        or fallback
    )


def route_label_for_request(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path or "/"
    return str(path)


def classify_exception_result(exc: Exception) -> str:
    message = str(exc).lower()
    if "timeout" in message:
        return "timeout"
    return "error"


class AppMetrics:
    def __init__(self, app_name: str) -> None:
        self.app_name = normalize_metric_label(app_name)
        self.registry = CollectorRegistry()
        self._redundancy_modes = ("primary", "failed_over", "degraded", "disabled")
        self.http_requests_total = Counter(
            "ncoai_http_requests_total",
            "Total HTTP requests handled by the application.",
            labelnames=("app", "method", "path", "status"),
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "ncoai_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            labelnames=("app", "method", "path"),
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.auth_attempts_total = Counter(
            "ncoai_auth_attempts_total",
            "Authentication attempts by source and result.",
            labelnames=("app", "source", "result"),
            registry=self.registry,
        )
        self.quota_events_total = Counter(
            "ncoai_quota_events_total",
            "Quota-related events by scope and result.",
            labelnames=("app", "scope", "result"),
            registry=self.registry,
        )
        self.upstream_requests_total = Counter(
            "ncoai_upstream_requests_total",
            "Upstream requests by dependency and result.",
            labelnames=("app", "upstream", "result"),
            registry=self.registry,
        )
        self.vllm_redundancy_enabled = Gauge(
            "ncoai_vllm_redundancy_enabled",
            "Whether vLLM failover monitoring is enabled.",
            labelnames=("app", "service"),
            registry=self.registry,
        )
        self.vllm_redundancy_mode = Gauge(
            "ncoai_vllm_redundancy_mode",
            "Current vLLM redundancy mode by state.",
            labelnames=("app", "service", "mode"),
            registry=self.registry,
        )
        self.vllm_redundancy_endpoint_health = Gauge(
            "ncoai_vllm_redundancy_endpoint_health",
            "Endpoint health for the primary and backup vLLM nodes.",
            labelnames=("app", "service", "endpoint"),
            registry=self.registry,
        )
        self.vllm_redundancy_process_running = Gauge(
            "ncoai_vllm_redundancy_process_running",
            "Whether the local managed vLLM process is running.",
            labelnames=("app", "service"),
            registry=self.registry,
        )
        self.vllm_redundancy_restart_budget_exhausted = Gauge(
            "ncoai_vllm_redundancy_restart_budget_exhausted",
            "Whether the automatic vLLM restart budget is exhausted.",
            labelnames=("app", "service"),
            registry=self.registry,
        )
        self.vllm_redundancy_transitions_total = Counter(
            "ncoai_vllm_redundancy_transitions_total",
            "Transition events for vLLM redundancy state changes.",
            labelnames=("app", "service", "event"),
            registry=self.registry,
        )
        self.vllm_redundancy_restart_attempts_total = Counter(
            "ncoai_vllm_redundancy_restart_attempts_total",
            "Automatic local vLLM restart attempts by result.",
            labelnames=("app", "service", "result"),
            registry=self.registry,
        )

    async def instrument_http(self, request: Request, call_next: Any) -> Response:
        method = request.method.upper()
        started_at = monotonic()
        response: Response | None = None
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        except Exception:
            raise
        finally:
            path = route_label_for_request(request)
            self.http_requests_total.labels(
                app=self.app_name,
                method=method,
                path=path,
                status=status,
            ).inc()
            self.http_request_duration_seconds.labels(
                app=self.app_name,
                method=method,
                path=path,
            ).observe(monotonic() - started_at)

    def record_auth_attempt(self, source: str, result: str) -> None:
        self.auth_attempts_total.labels(
            app=self.app_name,
            source=normalize_metric_label(source),
            result=normalize_metric_label(result),
        ).inc()

    def record_quota_event(self, scope: str | None, result: str) -> None:
        self.quota_events_total.labels(
            app=self.app_name,
            scope=normalize_metric_label(scope, fallback="unknown"),
            result=normalize_metric_label(result),
        ).inc()

    def record_upstream_request(self, upstream: str, result: str) -> None:
        self.upstream_requests_total.labels(
            app=self.app_name,
            upstream=normalize_metric_label(upstream),
            result=normalize_metric_label(result),
        ).inc()

    def update_vllm_redundancy_state(
        self,
        *,
        service: str,
        enabled: bool,
        mode: str,
        primary_healthy: bool,
        primary_process_running: bool,
        backup_healthy: bool | None,
        restart_budget_exhausted: bool,
    ) -> None:
        service_label = normalize_metric_label(service)
        self.vllm_redundancy_enabled.labels(
            app=self.app_name,
            service=service_label,
        ).set(1 if enabled else 0)
        self.vllm_redundancy_process_running.labels(
            app=self.app_name,
            service=service_label,
        ).set(1 if primary_process_running else 0)
        self.vllm_redundancy_restart_budget_exhausted.labels(
            app=self.app_name,
            service=service_label,
        ).set(1 if restart_budget_exhausted else 0)
        self.vllm_redundancy_endpoint_health.labels(
            app=self.app_name,
            service=service_label,
            endpoint="primary",
        ).set(1 if primary_healthy else 0)
        self.vllm_redundancy_endpoint_health.labels(
            app=self.app_name,
            service=service_label,
            endpoint="backup",
        ).set(1 if backup_healthy else 0)
        normalized_mode = normalize_metric_label(mode, fallback="disabled")
        for candidate in self._redundancy_modes:
            self.vllm_redundancy_mode.labels(
                app=self.app_name,
                service=service_label,
                mode=candidate,
            ).set(1 if normalized_mode == candidate else 0)

    def record_vllm_redundancy_transition(self, *, service: str, event: str) -> None:
        self.vllm_redundancy_transitions_total.labels(
            app=self.app_name,
            service=normalize_metric_label(service),
            event=normalize_metric_label(event),
        ).inc()

    def record_vllm_redundancy_restart_attempt(
        self,
        *,
        service: str,
        result: str,
    ) -> None:
        self.vllm_redundancy_restart_attempts_total.labels(
            app=self.app_name,
            service=normalize_metric_label(service),
            result=normalize_metric_label(result),
        ).inc()

    def render_metrics(self) -> Response:
        return Response(
            content=generate_latest(self.registry),
            media_type=CONTENT_TYPE_LATEST,
        )
