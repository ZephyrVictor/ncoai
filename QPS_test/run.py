#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from QPS_test.browser_smoke import smoke_test
from QPS_test.common import ROOT_DIR, UserState, build_run_config, utcnow, write_json
from QPS_test.controlplane import ManagedUserBatchManager
from QPS_test.reporting import RequestRecorder, build_summary, write_report_artifacts
from QPS_test.runtime import (
    HostEnvOverrideManager,
    LogCapMonitor,
    assert_devctl_ok,
    build_runtime_overrides,
    dump_runtime_state,
    ensure_services_ready,
    wait_for_service_urls,
)
from QPS_test.protocol import LoadGenerator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QPS_test real Chainlit flow load test")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--mode", default="conversation")
    parser.add_argument("--users", type=int, default=120)
    parser.add_argument("--target-rps", type=int, default=120)
    parser.add_argument("--per-user-default-inflight", type=int, default=1)
    parser.add_argument("--per-user-max-inflight", type=int, default=1)
    parser.add_argument("--traffic-profile", default="chat_availability")
    parser.add_argument("--log-cap-gb", type=float, default=100.0)
    parser.add_argument("--keep-users", action="store_true")
    parser.add_argument("--skip-browser-smoke", action="store_true")
    parser.add_argument("--input-token-target", type=int, default=200)
    parser.add_argument("--request-token-target", type=int)
    parser.add_argument("--stop-ratio", type=float, default=0.05)
    parser.add_argument("--user-limit-tokens-override", type=int)
    parser.add_argument("--thread-limit-tokens-override", type=int)
    parser.add_argument("--request-timeout-seconds", type=float, default=240.0)
    parser.add_argument("--smoke-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--progress-interval-seconds", type=float, default=5.0)
    parser.add_argument("--login-batch-size", type=int, default=10)
    parser.add_argument("--log-cap-warning-ratio", type=float, default=0.9)
    parser.add_argument("--max-global-inflight", type=int, default=120)
    parser.add_argument("--conversation-start-jitter-ms", type=int, default=3000)
    parser.add_argument("--ramp-start-rps", type=int, default=5)
    parser.add_argument("--ramp-step-seconds", type=float, default=30.0)
    parser.add_argument("--transport-failure-window", type=int, default=30)
    parser.add_argument("--transport-failure-abort-ratio", type=float, default=0.20)
    parser.add_argument("--failure-window-size", type=int, default=30)
    parser.add_argument("--failure-abort-ratio", type=float, default=0.20)
    parser.add_argument("--consecutive-failure-abort-count", type=int, default=12)
    parser.add_argument("--resource-sample-seconds", type=float, default=5.0)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    return parser


async def orchestrate(args: argparse.Namespace) -> int:
    config = build_run_config(args)
    recorder = RequestRecorder(config)
    override_manager = HostEnvOverrideManager(path=ROOT_DIR / "infra" / "env" / "host.env")
    user_manager = ManagedUserBatchManager(config)
    log_monitor = LogCapMonitor(
        max_bytes=config.log_cap_bytes,
        warning_ratio=config.log_cap_warning_ratio,
    )
    users_deleted = 0
    smoke_result = None
    load_result: dict[str, Any] = {
        "started_at": None,
        "finished_at": None,
        "aborted_due_to_log_cap": False,
        "log_cap_warning_triggered": False,
        "log_size_bytes": 0,
    }
    workflow_error: str | None = None
    credentials = []
    workflow_started_at = utcnow()

    try:
        print("[INFO] QPS_test preflight: checking existing services", flush=True)
        await ensure_services_ready(["vllm", "litellm", "chainlit", "admin"])

        override_manager.apply(build_runtime_overrides(config))
        write_json(config.paths.state_json, dump_runtime_state(config, override_manager))

        print(
            "[INFO] QPS_test runtime switch: CHAT_AUTH_MODE=managed_user, LITELLM_SET_VERBOSE=0",
            flush=True,
        )
        assert_devctl_ok(["down", "chainlit", "litellm"])
        assert_devctl_ok(["up", "litellm", "chainlit"])
        await wait_for_service_urls(["litellm", "chainlit"])

        print(f"[INFO] QPS_test creating {config.users} managed users", flush=True)
        credentials = await user_manager.create_users(user_count=config.users)
        user_states = [UserState(credential=item) for item in credentials]

        if not config.skip_browser_smoke:
            print("[INFO] QPS_test running browser smoke", flush=True)
            smoke_result = await smoke_test(config=config, credential=credentials[0])
            if not (smoke_result.login_ok and smoke_result.stream_ok):
                raise RuntimeError(
                    "browser smoke failed; see QPS_test browser_smoke artifacts for details"
                )
            if not smoke_result.success:
                print(
                    "[WARN] QPS_test browser smoke passed the required checks "
                    "but stop/thread-switch verification was incomplete; continuing",
                    flush=True,
                )

        print("[INFO] QPS_test starting protocol load generator", flush=True)
        generator = LoadGenerator(
            config=config,
            users=user_states,
            recorder=recorder,
            log_monitor=log_monitor,
        )
        load_result = await generator.run()
    except Exception as exc:
        workflow_error = str(exc)
        print(f"[ERROR] QPS_test failed: {workflow_error}", file=sys.stderr, flush=True)
    finally:
        try:
            if credentials and not config.keep_users:
                print("[INFO] QPS_test soft deleting managed users", flush=True)
                users_deleted = await user_manager.soft_delete_users(credentials)
        finally:
            await user_manager.close()

        print("[INFO] QPS_test restoring runtime overrides", flush=True)
        override_manager.restore()
        try:
            assert_devctl_ok(["down", "chainlit", "litellm"])
            assert_devctl_ok(["up", "litellm", "chainlit"])
            await wait_for_service_urls(["litellm", "chainlit"])
        except Exception as exc:
            restore_error = str(exc)
            workflow_error = (
                f"{workflow_error} | restore_failed: {restore_error}"
                if workflow_error
                else f"restore_failed: {restore_error}"
            )
            print(f"[ERROR] QPS_test restore failed: {restore_error}", file=sys.stderr, flush=True)

    summary = build_summary(
        config=config,
        started_at=load_result["started_at"] or workflow_started_at,
        finished_at=load_result["finished_at"] or utcnow(),
        recorder=recorder,
        smoke_result=smoke_result,
        users_deleted=users_deleted,
        aborted_due_to_log_cap=bool(load_result["aborted_due_to_log_cap"]),
        log_cap_warning_triggered=bool(load_result["log_cap_warning_triggered"]),
        log_size_bytes=int(load_result["log_size_bytes"]),
    )
    if workflow_error:
        summary["workflow_error"] = workflow_error
    if load_result.get("abort_reason"):
        summary["abort_reason"] = load_result["abort_reason"]
    write_report_artifacts(
        config=config,
        recorder=recorder,
        summary=summary,
        smoke_result=smoke_result,
    )
    print(f"[INFO] QPS_test artifacts written to {config.paths.root}", flush=True)
    return 1 if workflow_error else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(orchestrate(args))


if __name__ == "__main__":
    raise SystemExit(main())
