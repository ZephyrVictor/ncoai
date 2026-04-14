from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

OPS_SERVICE_SPEC = importlib.util.spec_from_file_location(
    "ncoai_admin_ops_service_for_tests",
    APP_DIR / "ops_service.py",
)
assert OPS_SERVICE_SPEC and OPS_SERVICE_SPEC.loader
ops_service = importlib.util.module_from_spec(OPS_SERVICE_SPEC)
OPS_SERVICE_SPEC.loader.exec_module(ops_service)


def test_build_logs_query_defaults_to_host_services_and_exception_levels() -> None:
    query = ops_service.build_logs_query()

    assert (
        query
        == '{job="host-services", service=~"admin|chainlit|litellm|vllm", level=~"error|critical|fatal"}'
    )


def test_build_logs_query_restricts_services_to_supported_host_apps() -> None:
    query = ops_service.build_logs_query(
        service="litellm,postgres,admin",
        level="error",
        text_query="failed",
    )

    assert (
        query
        == '{job="host-services", service=~"litellm|admin", level=~"error"} |= "failed"'
    )


def test_build_logs_query_returns_no_match_for_unsupported_services() -> None:
    query = ops_service.build_logs_query(service="postgres,redis")

    assert (
        query
        == '{job="host-services", service=~"^$", level=~"error|critical|fatal"}'
    )


def test_ops_log_window_clamps_to_shanghai_start_of_day() -> None:
    now = datetime(2026, 4, 8, 18, 30, tzinfo=timezone.utc)

    start, end = ops_service._ops_log_window(hours=12, now=now)

    assert start == datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    assert end == now
