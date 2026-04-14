from __future__ import annotations

from pathlib import Path

from QPS_test.common import build_run_config
from QPS_test.runtime import (
    HostEnvOverrideManager,
    LogCapMonitor,
    ResourceSafetyMonitor,
    SafetySnapshot,
    build_runtime_overrides,
    parse_percent_value,
    render_env_assignments,
    service_probe_urls,
    strip_managed_overrides,
)


def test_render_env_assignments_quotes_values() -> None:
    rendered = render_env_assignments({"CHAT_AUTH_MODE": "managed_user", "LITELLM_SET_VERBOSE": "0"})
    assert 'CHAT_AUTH_MODE="managed_user"' in rendered
    assert 'LITELLM_SET_VERBOSE="0"' in rendered


def test_host_env_override_manager_roundtrips(tmp_path: Path) -> None:
    target = tmp_path / "host.env"
    target.write_text("MODEL_PATH=/tmp/model\n", encoding="utf-8")
    manager = HostEnvOverrideManager(path=target)

    manager.apply({"CHAT_AUTH_MODE": "managed_user", "LITELLM_SET_VERBOSE": "0"})
    updated = target.read_text(encoding="utf-8")
    assert "MODEL_PATH=/tmp/model" in updated
    assert "CHAT_AUTH_MODE=\"managed_user\"" in updated

    manager.restore()
    assert target.read_text(encoding="utf-8") == "MODEL_PATH=/tmp/model\n"


def test_strip_managed_overrides_removes_trailing_qps_block() -> None:
    original = "MODEL_PATH=/tmp/model\n\n# QPS_test managed overrides\nCHAT_AUTH_MODE=\"managed_user\"\n"
    assert strip_managed_overrides(original) == "MODEL_PATH=/tmp/model\n"


def test_log_cap_monitor_triggers_warning_and_abort(monkeypatch) -> None:
    monkeypatch.setattr("QPS_test.runtime.directory_size_bytes", lambda _path: 95)
    monitor = LogCapMonitor(max_bytes=100, warning_ratio=0.9)
    status = monitor.check()
    assert status.warning_triggered is True
    assert status.abort_triggered is False

    monkeypatch.setattr("QPS_test.runtime.directory_size_bytes", lambda _path: 100)
    status = monitor.check()
    assert status.abort_triggered is True


def test_parse_percent_value_handles_expected_inputs() -> None:
    assert parse_percent_value("85.5%") == 0.855
    assert parse_percent_value("0") == 0.0
    assert parse_percent_value("") is None


def test_service_probe_urls_use_lightweight_litellm_probe() -> None:
    env = {"LITELLM_PORT": "33455"}
    assert service_probe_urls("litellm", env) == ["http://127.0.0.1:33455/v1/models"]


class _Args:
    run_id = "test-run"
    mode = "conversation"
    users = 2
    target_rps = 120
    per_user_default_inflight = 1
    per_user_max_inflight = 1
    traffic_profile = "standard"
    log_cap_gb = 100.0
    keep_users = False
    skip_browser_smoke = False
    input_token_target = 200
    request_token_target = 500
    stop_ratio = 0.05
    request_timeout_seconds = 240.0
    smoke_timeout_seconds = 90.0
    progress_interval_seconds = 5.0
    login_batch_size = 10
    log_cap_warning_ratio = 0.9
    max_global_inflight = 2
    conversation_start_jitter_ms = 3000
    ramp_start_rps = 5
    ramp_step_seconds = 30.0
    transport_failure_window = 30
    transport_failure_abort_ratio = 0.20
    failure_window_size = 30
    failure_abort_ratio = 0.20
    consecutive_failure_abort_count = 12
    resource_sample_seconds = 5.0
    duration_seconds = 300.0
    user_limit_tokens_override = None
    thread_limit_tokens_override = None


def test_build_runtime_overrides_pins_completion_tokens_to_500() -> None:
    config = build_run_config(_Args())
    overrides = build_runtime_overrides(config)
    assert overrides["CHAT_AUTH_MODE"] == "managed_user"
    assert overrides["LITELLM_SET_VERBOSE"] == "0"
    assert overrides["CHAINLIT_QUOTA_DEFAULT_MAX_COMPLETION_TOKENS"] == "500"


def test_resource_safety_monitor_aborts_after_consecutive_host_memory_breaches() -> None:
    config = build_run_config(_Args())
    monitor = ResourceSafetyMonitor(config)
    snapshot = SafetySnapshot(
        service_health={
            "chainlit": (True, None),
            "litellm": (True, None),
            "vllm": (True, None),
        },
        host_memory_ratio=0.95,
        postgres_memory_ratio=0.10,
        redis_memory_ratio=0.10,
        gpu_memory_ratio=0.10,
        gpu_probe_failed=False,
    )

    assert monitor.evaluate(snapshot) is None
    assert monitor.evaluate(snapshot) is None
    assert "host memory" in (monitor.evaluate(snapshot) or "")
