#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from QPS_test.common import build_run_config, utcnow, write_json
from QPS_test.controlplane import ManagedUserBatchManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare managed users for the next QPS_test run."
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--purge-prefix", default="qps-test-")
    parser.add_argument("--login-batch-size", type=int, default=10)
    parser.add_argument("--keep-existing-users", action="store_true")
    parser.add_argument("--purge-only", action="store_true")
    return parser


def build_config(args: argparse.Namespace):
    config_args = argparse.Namespace(
        run_id=args.run_id,
        mode="conversation",
        users=args.users,
        target_rps=120,
        per_user_default_inflight=1,
        per_user_max_inflight=1,
        traffic_profile="chat_availability",
        log_cap_gb=100.0,
        keep_users=False,
        skip_browser_smoke=False,
        input_token_target=200,
        request_token_target=None,
        stop_ratio=0.05,
        user_limit_tokens_override=None,
        thread_limit_tokens_override=None,
        request_timeout_seconds=240.0,
        smoke_timeout_seconds=90.0,
        progress_interval_seconds=5.0,
        login_batch_size=args.login_batch_size,
        log_cap_warning_ratio=0.9,
        max_global_inflight=max(args.users, 120),
        conversation_start_jitter_ms=3000,
        ramp_start_rps=5,
        ramp_step_seconds=30.0,
        transport_failure_window=30,
        transport_failure_abort_ratio=0.20,
        failure_window_size=30,
        failure_abort_ratio=0.20,
        consecutive_failure_abort_count=12,
        resource_sample_seconds=5.0,
        duration_seconds=300.0,
    )
    return build_run_config(config_args)


async def prepare(args: argparse.Namespace) -> int:
    config = build_config(args)
    manager = ManagedUserBatchManager(config)
    created = []

    try:
        before = await manager.list_prefixed_users(
            identifier_prefix=args.purge_prefix,
            include_deleted=True,
        )
        active_before = sum(1 for item in before if not bool(item.get("is_deleted")))
        deleted_before = len(before) - active_before

        purged = 0
        if not args.keep_existing_users:
            purged = await manager.soft_delete_prefixed_users(
                identifier_prefix=args.purge_prefix,
            )

        if not args.purge_only:
            created = await manager.create_users(user_count=config.users)

        after = await manager.list_prefixed_users(
            identifier_prefix=args.purge_prefix,
            include_deleted=True,
        )
        active_after = sum(1 for item in after if not bool(item.get("is_deleted")))
        deleted_after = len(after) - active_after

        summary = {
            "prepared_at": utcnow().isoformat(),
            "run_id": config.run_id,
            "purge_prefix": args.purge_prefix,
            "users_requested": config.users,
            "purged_users": purged,
            "created_users": len(created),
            "active_before": active_before,
            "deleted_before": deleted_before,
            "active_after": active_after,
            "deleted_after": deleted_after,
            "users_json": str(config.paths.users_json) if created else None,
            "shared_password": created[0].password if created else None,
            "credentials_preview": [asdict(item) for item in created[:3]],
        }
        summary_path = config.paths.root / "prepare_users.json"
        write_json(summary_path, summary)

        print(f"[INFO] Prepared run_id: {config.run_id}", flush=True)
        print(f"[INFO] Purged old users: {purged}", flush=True)
        print(f"[INFO] Created new users: {len(created)}", flush=True)
        print(f"[INFO] Active qps-test users after prep: {active_after}", flush=True)
        print(f"[INFO] Summary: {summary_path}", flush=True)
        if created:
            print(f"[INFO] Users inventory: {config.paths.users_json}", flush=True)
            print(f"[INFO] Shared password: {created[0].password}", flush=True)
        return 0
    finally:
        await manager.close()


def main() -> int:
    return asyncio.run(prepare(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
