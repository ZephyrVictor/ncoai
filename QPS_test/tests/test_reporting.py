from __future__ import annotations

from datetime import timedelta

from QPS_test.common import BrowserSmokeResult, RequestRecord, build_run_config, utcnow
from QPS_test.reporting import RequestRecorder, build_summary


class _Args:
    run_id = "test-run"
    mode = "conversation"
    users = 2
    target_rps = 10
    per_user_default_inflight = 1
    per_user_max_inflight = 1
    traffic_profile = "chat_availability"
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


def _record(
    *,
    user_id: str,
    request_seq: int,
    success: bool,
    behavior_type: str = "normal",
    ttft_ms: float | None = 100.0,
    wall_time_ms: float | None = 500.0,
    total_tokens: int | None = 900,
    quota_scope: str | None = None,
) -> RequestRecord:
    now = utcnow()
    return RequestRecord(
        run_id="test-run",
        user_id=user_id,
        thread_id=f"thread-{user_id}",
        session_id=f"session-{user_id}-{request_seq}",
        conversation_id=f"thread-{user_id}",
        behavior_type=behavior_type,
        request_seq=request_seq,
        conversation_turn_index=request_seq,
        session_rollover_index=1,
        request_sent_at=now.isoformat(),
        first_token_at=now.isoformat() if ttft_ms is not None else None,
        task_end_at=now.isoformat(),
        ttft_ms=ttft_ms,
        wall_time_ms=wall_time_ms,
        generation_duration_ms=200.0 if success else None,
        output_tokens_per_second=1.5 if success else None,
        stop_sent_at=None,
        success=success,
        failure_type=None if success else "quota_blocked",
        http_or_ws_error=None,
        input_tokens_estimated=200,
        prompt_tokens=600,
        completion_tokens=300,
        completion_tokens_reported_or_estimated=300,
        total_tokens=total_tokens,
        stopped_by_disturbance=False,
        stop_trigger_tokens_estimated=None,
        natural_completion=success,
        session_rollover_reason="initial_thread" if request_seq == 1 else None,
        quota_blocked_scope=quota_scope,
        quota_blocked_reason="quota hit" if quota_scope else None,
    )


def test_build_summary_aggregates_success_failure_and_quota() -> None:
    config = build_run_config(_Args())
    recorder = RequestRecorder(config)
    recorder.record(_record(user_id="u1", request_seq=1, success=True, behavior_type="normal"))
    recorder.record(_record(user_id="u1", request_seq=2, success=False, behavior_type="stop", quota_scope="user"))
    recorder.record(_record(user_id="u2", request_seq=1, success=True, behavior_type="thread_switch"))

    smoke = BrowserSmokeResult(
        success=True,
        login_ok=True,
        stream_ok=True,
        stop_ok=True,
        thread_switch_ok=True,
        screenshots=["01.png"],
        notes=[],
    )
    started_at = utcnow()
    finished_at = started_at + timedelta(seconds=2)
    summary = build_summary(
        config=config,
        started_at=started_at,
        finished_at=finished_at,
        recorder=recorder,
        smoke_result=smoke,
        users_deleted=2,
        aborted_due_to_log_cap=False,
        log_cap_warning_triggered=False,
        log_size_bytes=123,
    )

    assert summary["total_requests"] == 3
    assert summary["success_count"] == 2
    assert summary["failure_count"] == 1
    assert summary["blocked_user_count"] == 1
    assert summary["smoke_result"]["success"] is True
    assert summary["availability_passed"] is False
    assert summary["normal_success_rate"] == 1.0
    assert summary["users_with_successful_normal_chat_rate"] == 0.5
    assert summary["stop_clean_completion_rate"] == 0.0


def test_build_summary_passes_availability_mode_when_normal_chat_is_healthy() -> None:
    config = build_run_config(_Args())
    recorder = RequestRecorder(config)
    recorder.record(_record(user_id="u1", request_seq=1, success=True, behavior_type="normal"))
    recorder.record(
        RequestRecord(
            run_id="test-run",
            user_id="u1",
            thread_id="thread-u1",
            session_id="session-u1-2",
            conversation_id="thread-u1",
            behavior_type="stop",
            request_seq=2,
            conversation_turn_index=2,
            session_rollover_index=1,
            request_sent_at=utcnow().isoformat(),
            first_token_at=None,
            task_end_at=utcnow().isoformat(),
            ttft_ms=None,
            wall_time_ms=300.0,
            generation_duration_ms=None,
            output_tokens_per_second=None,
            stop_sent_at=utcnow().isoformat(),
            success=True,
            failure_type=None,
            http_or_ws_error=None,
            input_tokens_estimated=200,
            prompt_tokens=100,
            completion_tokens=50,
            completion_tokens_reported_or_estimated=50,
            total_tokens=150,
            stopped_by_disturbance=True,
            stop_trigger_tokens_estimated=64,
            natural_completion=False,
            session_rollover_reason=None,
            quota_blocked_scope=None,
            quota_blocked_reason=None,
        )
    )
    recorder.record(_record(user_id="u2", request_seq=1, success=True, behavior_type="normal"))

    smoke = BrowserSmokeResult(
        success=True,
        login_ok=True,
        stream_ok=True,
        stop_ok=True,
        thread_switch_ok=True,
        screenshots=[],
        notes=[],
    )
    started_at = utcnow()
    finished_at = started_at + timedelta(seconds=5)
    summary = build_summary(
        config=config,
        started_at=started_at,
        finished_at=finished_at,
        recorder=recorder,
        smoke_result=smoke,
        users_deleted=0,
        aborted_due_to_log_cap=False,
        log_cap_warning_triggered=False,
        log_size_bytes=0,
    )

    assert summary["availability_passed"] is True
    assert summary["availability_failed_checks"] == []
    assert summary["normal_success_rate"] == 1.0
    assert summary["users_with_successful_normal_chat_rate"] == 1.0
    assert summary["stop_clean_completion_rate"] == 1.0
