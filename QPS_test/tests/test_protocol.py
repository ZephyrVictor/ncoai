from __future__ import annotations

from datetime import timedelta

from QPS_test.common import ManagedUserCredential, RequestRecord, UserState, build_run_config, utcnow
from QPS_test.protocol import LoadGenerator
from QPS_test.reporting import RequestRecorder
from QPS_test.runtime import LogCapMonitor


class _Args:
    run_id = "test-run"
    mode = "legacy_rps"
    users = 2
    target_rps = 120
    per_user_default_inflight = 2
    per_user_max_inflight = 5
    traffic_profile = "standard"
    log_cap_gb = 100.0
    keep_users = False
    skip_browser_smoke = True
    input_token_target = 200
    request_token_target = 500
    stop_ratio = 0.05
    request_timeout_seconds = 240.0
    smoke_timeout_seconds = 90.0
    progress_interval_seconds = 5.0
    login_batch_size = 10
    log_cap_warning_ratio = 0.9
    max_global_inflight = 48
    conversation_start_jitter_ms = 3000
    ramp_start_rps = 5
    ramp_step_seconds = 30.0
    transport_failure_window = 4
    transport_failure_abort_ratio = 0.5
    failure_window_size = 4
    failure_abort_ratio = 0.5
    consecutive_failure_abort_count = 12
    resource_sample_seconds = 5.0
    duration_seconds = 0.0
    user_limit_tokens_override = None
    thread_limit_tokens_override = None


def _build_generator() -> LoadGenerator:
    config = build_run_config(_Args())
    return LoadGenerator(
        config=config,
        users=[],
        recorder=RequestRecorder(config),
        log_monitor=LogCapMonitor(max_bytes=100, warning_ratio=0.9),
    )


def _record(*, failure_type: str | None, success: bool) -> RequestRecord:
    now = utcnow().isoformat()
    return RequestRecord(
        run_id="test-run",
        user_id="u1",
        thread_id="t1",
        session_id="s1",
        conversation_id="t1",
        behavior_type="normal",
        request_seq=1,
        conversation_turn_index=1,
        session_rollover_index=1,
        request_sent_at=now,
        first_token_at=now if success else None,
        task_end_at=now,
        ttft_ms=10.0 if success else None,
        wall_time_ms=20.0,
        generation_duration_ms=10.0 if success else None,
        output_tokens_per_second=1.0 if success else None,
        stop_sent_at=None,
        success=success,
        failure_type=failure_type,
        http_or_ws_error="" if failure_type else None,
        input_tokens_estimated=200,
        prompt_tokens=10,
        completion_tokens=10,
        completion_tokens_reported_or_estimated=10,
        total_tokens=20,
        stopped_by_disturbance=False,
        stop_trigger_tokens_estimated=None,
        natural_completion=success,
        session_rollover_reason="initial_thread",
        quota_blocked_scope=None,
        quota_blocked_reason=None,
    )


def test_current_target_rps_uses_safe_ramp_up() -> None:
    generator = _build_generator()
    started_at = utcnow()
    assert generator._current_target_rps(started_at) == 5
    assert generator._current_target_rps(started_at - timedelta(seconds=30)) == 10
    assert generator._current_target_rps(started_at - timedelta(seconds=60)) == 20
    assert generator._current_target_rps(started_at - timedelta(seconds=90)) == 40
    assert generator._current_target_rps(started_at - timedelta(seconds=120)) == 80
    assert generator._current_target_rps(started_at - timedelta(seconds=150)) == 120


def test_transport_failure_window_triggers_safe_abort() -> None:
    generator = _build_generator()
    generator._record_transport_failure(_record(failure_type="http_error", success=False))
    generator._record_transport_failure(_record(failure_type="ws_error", success=False))
    generator._record_transport_failure(_record(failure_type=None, success=True))
    assert generator.abort_event.is_set() is False

    generator._record_transport_failure(_record(failure_type="timeout", success=False))
    assert generator.abort_event.is_set() is True
    assert generator._abort_reason is not None


def test_quota_rollover_does_not_count_as_consecutive_failure_abort() -> None:
    args = type(
        "_QuotaArgs",
        (),
        {**_Args.__dict__, "consecutive_failure_abort_count": 2},
    )()
    config = build_run_config(args)
    generator = LoadGenerator(
        config=config,
        users=[],
        recorder=RequestRecorder(config),
        log_monitor=LogCapMonitor(max_bytes=100, warning_ratio=0.9),
    )
    record = _record(failure_type="quota_blocked", success=False)
    record.quota_blocked_scope = "thread"

    generator._record_transport_failure(record)
    generator._record_transport_failure(record)

    assert generator.abort_event.is_set() is False


def test_duration_reached_respects_zero_and_positive_limits() -> None:
    generator = _build_generator()
    started_at = utcnow()
    assert generator._duration_reached(started_at) is False

    generator.config = build_run_config(
        type(
            "_ArgsWithDuration",
            (),
            {**_Args.__dict__, "duration_seconds": 10.0},
        )()
    )
    assert generator._duration_reached(started_at) is False
    assert generator._duration_reached(started_at - timedelta(seconds=11)) is True


def test_select_conversation_thread_rolls_over_when_thread_headroom_is_exhausted() -> None:
    config = build_run_config(
        type(
            "_ConversationArgs",
            (),
            {**_Args.__dict__, "mode": "conversation", "target_rps": 0},
        )()
    )
    generator = LoadGenerator(
        config=config,
        users=[],
        recorder=RequestRecorder(config),
        log_monitor=LogCapMonitor(max_bytes=100, warning_ratio=0.9),
    )
    user = UserState(
        credential=ManagedUserCredential(
            identifier="u1",
            password="secret",
            display_name="u1",
            run_id="test-run",
            created_at=utcnow().isoformat(),
        )
    )

    first = generator._select_conversation_thread(user)
    first.thread_id = "thread-1"
    first.session_used_tokens = config.thread_limit_tokens - config.minimum_request_tokens

    second = generator._select_conversation_thread(user)

    assert first is not second
    assert second.rollover_index == 2
    assert second.pending_rollover_reason == "thread_token_limit_predicted"
