from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from QPS_test.common import (
    BrowserSmokeResult,
    RequestRecord,
    RunConfig,
    append_jsonl,
    percentile,
    serialize_datetime,
    write_json,
    write_latency_csv,
    write_text,
)

TRANSPORT_FAILURE_TYPES = {"http_error", "ws_error", "connect_error", "timeout"}
NORMAL_SUCCESS_RATE_THRESHOLD = 0.99
TRANSPORT_FAILURE_RATE_THRESHOLD = 0.01
STOP_CLEAN_COMPLETION_RATE_THRESHOLD = 0.95
NORMAL_USER_COVERAGE_RATE_THRESHOLD = 0.99


class RequestRecorder:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.rows: list[RequestRecord] = []

    def record(self, row: RequestRecord) -> None:
        self.rows.append(row)
        append_jsonl(self.config.paths.requests_jsonl, row.to_dict())
        if not row.success:
            append_jsonl(self.config.paths.failures_jsonl, row.to_dict())

    def finalize_latency_csv(self) -> None:
        write_latency_csv(self.config.paths.latency_csv, self.rows)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def build_summary(
    *,
    config: RunConfig,
    started_at: datetime,
    finished_at: datetime,
    recorder: RequestRecorder,
    smoke_result: BrowserSmokeResult | None,
    users_deleted: int,
    aborted_due_to_log_cap: bool,
    log_cap_warning_triggered: bool,
    log_size_bytes: int,
) -> dict[str, Any]:
    rows = recorder.rows
    success_rows = [row for row in rows if row.success]
    failure_rows = [row for row in rows if not row.success]
    failure_type_counts = Counter(row.failure_type or "none" for row in failure_rows)
    transport_failure_rows = [
        row for row in rows if (row.failure_type or "") in TRANSPORT_FAILURE_TYPES
    ]
    ttft_values = [row.ttft_ms for row in success_rows if row.ttft_ms is not None]
    wall_values = [row.wall_time_ms for row in success_rows if row.wall_time_ms is not None]
    generation_values = [
        row.generation_duration_ms
        for row in success_rows
        if row.generation_duration_ms is not None
    ]
    output_tps_values = [
        row.output_tokens_per_second
        for row in success_rows
        if row.output_tokens_per_second is not None
    ]
    token_values = [row.total_tokens for row in success_rows if row.total_tokens is not None]
    behavior_counts = Counter(row.behavior_type for row in rows)
    behavior_success = Counter(row.behavior_type for row in success_rows)
    normal_rows = [row for row in rows if row.behavior_type == "normal"]
    normal_success_rows = [row for row in normal_rows if row.success]
    stop_rows = [row for row in rows if row.behavior_type == "stop"]
    stop_clean_rows = [
        row
        for row in stop_rows
        if row.task_end_at is not None
        and (row.failure_type or "") not in TRANSPORT_FAILURE_TYPES
        and row.quota_blocked_scope is None
    ]
    stop_signal_rows = [row for row in stop_rows if row.stop_sent_at is not None]
    disturbance_stop_rows = [row for row in rows if row.stopped_by_disturbance]
    natural_completion_rows = [row for row in success_rows if row.natural_completion]
    blocked_users = {
        row.user_id
        for row in rows
        if row.quota_blocked_scope == "user"
    }

    request_buckets: dict[str, int] = defaultdict(int)
    for row in rows:
        sent_at = _parse_timestamp(row.request_sent_at)
        if sent_at is None:
            continue
        bucket = sent_at.replace(microsecond=0).isoformat()
        request_buckets[bucket] += 1

    conversation_by_user: dict[str, set[str]] = defaultdict(set)
    turns_by_conversation: dict[tuple[str, str], int] = defaultdict(int)
    success_requests_before_user_limit: list[int] = []
    token_before_user_limit: list[int] = []
    rows_by_user: dict[str, list[RequestRecord]] = defaultdict(list)
    users_with_successful_normal_chat = {
        row.user_id for row in normal_success_rows
    }
    for row in rows:
        rows_by_user[row.user_id].append(row)
        conversation_id = row.conversation_id or row.thread_id
        if conversation_id:
            conversation_by_user[row.user_id].add(conversation_id)
            turns_by_conversation[(row.user_id, conversation_id)] += 1

    for user_id, user_rows in rows_by_user.items():
        user_rows.sort(key=lambda item: item.request_seq)
        user_success = [row for row in user_rows if row.success]
        first_user_block = next(
            (row for row in user_rows if row.quota_blocked_scope == "user"),
            None,
        )
        if first_user_block is not None:
            prior_success = [
                row
                for row in user_success
                if row.request_seq < first_user_block.request_seq
            ]
        else:
            prior_success = user_success
        success_requests_before_user_limit.append(len(prior_success))
        token_before_user_limit.append(
            sum(row.total_tokens or 0 for row in prior_success)
        )

    post_stop_follow_up_successes = 0
    post_stop_follow_up_total = 0
    for user_rows in rows_by_user.values():
        for index, row in enumerate(user_rows):
            if not row.stopped_by_disturbance:
                continue
            if index + 1 >= len(user_rows):
                continue
            post_stop_follow_up_total += 1
            if user_rows[index + 1].success:
                post_stop_follow_up_successes += 1

    started = started_at
    finished = finished_at
    wall_seconds = max((finished - started).total_seconds(), 0.001)
    smoke_required_ok = (
        smoke_result is None or (smoke_result.login_ok and smoke_result.stream_ok)
    )

    normal_success_rate = (
        len(normal_success_rows) / len(normal_rows)
        if normal_rows
        else 0.0
    )
    transport_failure_rate = len(transport_failure_rows) / max(len(rows), 1)
    stop_clean_completion_rate = (
        len(stop_clean_rows) / len(stop_rows)
        if stop_rows
        else 1.0
    )
    stop_signal_rate = (
        len(stop_signal_rows) / len(stop_rows)
        if stop_rows
        else 0.0
    )
    natural_completion_rate = (
        len(natural_completion_rows) / len(success_rows)
        if success_rows
        else 0.0
    )
    disturbance_stop_rate = len(disturbance_stop_rows) / max(len(rows), 1)
    post_stop_next_turn_success_rate = (
        post_stop_follow_up_successes / post_stop_follow_up_total
        if post_stop_follow_up_total
        else 1.0
    )
    normal_user_coverage_rate = (
        len(users_with_successful_normal_chat) / max(config.users, 1)
    )
    session_rollover_count = max(
        sum(max(len(conversations) - 1, 0) for conversations in conversation_by_user.values()),
        0,
    )
    average_turns_per_session = (
        sum(turns_by_conversation.values()) / max(len(turns_by_conversation), 1)
    )
    availability_failed_checks: list[str] = []
    if not smoke_required_ok:
        availability_failed_checks.append("browser smoke login/stream failed")
    if normal_success_rate < NORMAL_SUCCESS_RATE_THRESHOLD:
        availability_failed_checks.append(
            "normal chat success rate below "
            f"{NORMAL_SUCCESS_RATE_THRESHOLD:.0%}"
        )
    if transport_failure_rate > TRANSPORT_FAILURE_RATE_THRESHOLD:
        availability_failed_checks.append(
            "transport failure rate above "
            f"{TRANSPORT_FAILURE_RATE_THRESHOLD:.0%}"
        )
    if normal_user_coverage_rate < NORMAL_USER_COVERAGE_RATE_THRESHOLD:
        availability_failed_checks.append(
            "successful normal-chat user coverage below "
            f"{NORMAL_USER_COVERAGE_RATE_THRESHOLD:.0%}"
        )
    if stop_rows and stop_clean_completion_rate < STOP_CLEAN_COMPLETION_RATE_THRESHOLD:
        availability_failed_checks.append(
            "stop clean-completion rate below "
            f"{STOP_CLEAN_COMPLETION_RATE_THRESHOLD:.0%}"
        )

    summary = {
        "run_id": config.run_id,
        "started_at": serialize_datetime(started),
        "finished_at": serialize_datetime(finished),
        "wall_time_seconds": wall_seconds,
        "target_rps": config.target_rps,
        "actual_average_rps": len(rows) / wall_seconds,
        "actual_peak_rps": max(request_buckets.values(), default=0),
        "total_requests": len(rows),
        "success_count": len(success_rows),
        "failure_count": len(failure_rows),
        "failure_type_counts": dict(failure_type_counts),
        "blocked_user_count": len(blocked_users),
        "transport_failure_count": len(transport_failure_rows),
        "transport_failure_rate": transport_failure_rate,
        "aborted_due_to_log_cap": aborted_due_to_log_cap,
        "log_cap_warning_triggered": log_cap_warning_triggered,
        "log_size_bytes": log_size_bytes,
        "behavior_counts": dict(behavior_counts),
        "behavior_success_counts": dict(behavior_success),
        "normal_request_count": len(normal_rows),
        "normal_success_count": len(normal_success_rows),
        "normal_success_rate": normal_success_rate,
        "stop_request_count": len(stop_rows),
        "stop_clean_completion_count": len(stop_clean_rows),
        "stop_clean_completion_rate": stop_clean_completion_rate,
        "stop_signal_count": len(stop_signal_rows),
        "stop_signal_rate": stop_signal_rate,
        "users_with_successful_normal_chat_count": len(users_with_successful_normal_chat),
        "users_with_successful_normal_chat_rate": normal_user_coverage_rate,
        "availability_passed": not availability_failed_checks,
        "availability_failed_checks": availability_failed_checks,
        "ttft_ms": {
            "p50": percentile(ttft_values, 0.50),
            "p90": percentile(ttft_values, 0.90),
            "p95": percentile(ttft_values, 0.95),
            "p99": percentile(ttft_values, 0.99),
        },
        "wall_time_ms": {
            "p50": percentile(wall_values, 0.50),
            "p90": percentile(wall_values, 0.90),
            "p95": percentile(wall_values, 0.95),
            "p99": percentile(wall_values, 0.99),
        },
        "generation_duration_ms": {
            "p50": percentile(generation_values, 0.50),
            "p90": percentile(generation_values, 0.90),
            "p95": percentile(generation_values, 0.95),
            "p99": percentile(generation_values, 0.99),
        },
        "output_tps": {
            "p50": percentile(output_tps_values, 0.50),
            "p90": percentile(output_tps_values, 0.90),
            "p95": percentile(output_tps_values, 0.95),
            "p99": percentile(output_tps_values, 0.99),
        },
        "average_total_tokens": (sum(token_values) / len(token_values)) if token_values else 0.0,
        "session_rollover_count": session_rollover_count,
        "average_turns_per_session": average_turns_per_session,
        "natural_completion_rate": natural_completion_rate,
        "disturbance_stop_rate": disturbance_stop_rate,
        "post_stop_next_turn_success_rate": post_stop_next_turn_success_rate,
        "stop_success_rate": (
            behavior_success.get("stop", 0) / behavior_counts.get("stop", 1)
        ),
        "thread_switch_success_rate": (
            behavior_success.get("thread_switch", 0)
            / behavior_counts.get("thread_switch", 1)
        ),
        "average_threads_per_user": (
            sum(len(conversations) for conversations in conversation_by_user.values())
            / max(len(conversation_by_user), 1)
        ),
        "average_success_requests_before_user_limit": (
            sum(success_requests_before_user_limit)
            / max(len(success_requests_before_user_limit), 1)
        ),
        "average_tokens_before_user_limit": (
            sum(token_before_user_limit) / max(len(token_before_user_limit), 1)
        ),
        "users_deleted": users_deleted,
        "smoke_result": smoke_result.to_dict() if smoke_result else None,
    }
    return summary


def render_report(
    *,
    config: RunConfig,
    summary: dict[str, Any],
    smoke_result: BrowserSmokeResult | None,
) -> str:
    lines = [
        f"# QPS Test Report: {config.run_id}",
        "",
        "## Overview",
        f"- Users: {config.users}",
        f"- Target RPS: {config.target_rps}",
        f"- Total requests: {summary['total_requests']}",
        f"- Success: {summary['success_count']}",
        f"- Failure: {summary['failure_count']}",
        f"- Blocked users: {summary['blocked_user_count']}",
        f"- Actual average RPS: {summary['actual_average_rps']:.2f}",
        f"- Actual peak RPS: {summary['actual_peak_rps']}",
        f"- Availability passed: {summary['availability_passed']}",
        f"- Log cap warning triggered: {summary['log_cap_warning_triggered']}",
        f"- Aborted due to log cap: {summary['aborted_due_to_log_cap']}",
        "",
        "## Availability Acceptance",
        f"- Normal chat success rate: {summary['normal_success_rate']:.4f}",
        f"- Transport failure rate: {summary['transport_failure_rate']:.4f}",
        f"- Successful normal-chat user coverage: {summary['users_with_successful_normal_chat_rate']:.4f}",
        f"- Stop clean-completion rate: {summary['stop_clean_completion_rate']:.4f}",
        f"- Stop signal rate: {summary['stop_signal_rate']:.4f}",
        f"- Failed checks: {', '.join(summary['availability_failed_checks']) if summary['availability_failed_checks'] else 'none'}",
        "",
        "## Latency",
        f"- TTFT p50/p90/p95/p99 (ms): {summary['ttft_ms']['p50']:.2f} / {summary['ttft_ms']['p90']:.2f} / {summary['ttft_ms']['p95']:.2f} / {summary['ttft_ms']['p99']:.2f}",
        f"- Wall p50/p90/p95/p99 (ms): {summary['wall_time_ms']['p50']:.2f} / {summary['wall_time_ms']['p90']:.2f} / {summary['wall_time_ms']['p95']:.2f} / {summary['wall_time_ms']['p99']:.2f}",
        f"- Generation p50/p90/p95/p99 (ms): {summary['generation_duration_ms']['p50']:.2f} / {summary['generation_duration_ms']['p90']:.2f} / {summary['generation_duration_ms']['p95']:.2f} / {summary['generation_duration_ms']['p99']:.2f}",
        f"- Output TPS p50/p90/p95/p99: {summary['output_tps']['p50']:.2f} / {summary['output_tps']['p90']:.2f} / {summary['output_tps']['p95']:.2f} / {summary['output_tps']['p99']:.2f}",
        "",
        "## Workload",
        f"- Session rollover count: {summary['session_rollover_count']}",
        f"- Average turns per session: {summary['average_turns_per_session']:.2f}",
        f"- Natural completion rate: {summary['natural_completion_rate']:.4f}",
        f"- Disturbance stop rate: {summary['disturbance_stop_rate']:.4f}",
        f"- Post-stop next-turn success rate: {summary['post_stop_next_turn_success_rate']:.4f}",
        f"- Stop success rate: {summary['stop_success_rate']:.4f}",
        f"- Thread switch success rate: {summary['thread_switch_success_rate']:.4f}",
        f"- Average threads per user: {summary['average_threads_per_user']:.2f}",
        f"- Average successful requests before user limit: {summary['average_success_requests_before_user_limit']:.2f}",
        f"- Average tokens before user limit: {summary['average_tokens_before_user_limit']:.2f}",
        "",
        "## Browser Smoke",
    ]
    if smoke_result is None:
        lines.append("- Skipped")
    else:
        lines.extend(
            [
                f"- Success: {smoke_result.success}",
                f"- Login: {smoke_result.login_ok}",
                f"- Stream: {smoke_result.stream_ok}",
                f"- Stop: {smoke_result.stop_ok}",
                f"- Thread switch: {smoke_result.thread_switch_ok}",
            ]
        )
        if smoke_result.screenshots:
            lines.append(f"- Screenshots: {', '.join(smoke_result.screenshots)}")
        if smoke_result.notes:
            lines.append(f"- Notes: {' | '.join(smoke_result.notes)}")
    lines.extend(
        [
            "",
            "## Artifacts",
            f"- requests.jsonl: `{config.paths.requests_jsonl}`",
            f"- failures.jsonl: `{config.paths.failures_jsonl}`",
            f"- latency.csv: `{config.paths.latency_csv}`",
            f"- summary.json: `{config.paths.summary_json}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report_artifacts(
    *,
    config: RunConfig,
    recorder: RequestRecorder,
    summary: dict[str, Any],
    smoke_result: BrowserSmokeResult | None,
) -> None:
    recorder.finalize_latency_csv()
    write_json(config.paths.summary_json, summary)
    write_text(
        config.paths.report_md,
        render_report(config=config, summary=summary, smoke_result=smoke_result),
    )
    if smoke_result is not None:
        write_json(config.paths.smoke_summary_json, smoke_result.to_dict())
