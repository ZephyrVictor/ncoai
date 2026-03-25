#!/usr/bin/env python3
from __future__ import annotations

import json
import os

import psycopg
from redis import Redis


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


DATABASE_URL = env(
    "DATABASE_URL",
    "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
)
REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/0")
TABLES = [
    "users",
    "threads",
    "steps",
    "admin_accounts",
    "managed_users",
    "quota_policies",
    "quota_usage_ledger",
    "quota_thread_summary",
    "quota_user_summary",
]


def main() -> int:
    report: dict[str, object] = {}
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
            existing_tables = {row[0] for row in cur.fetchall()}
            report["missing_tables"] = [table for table in TABLES if table not in existing_tables]
            cur.execute("SELECT COUNT(*) FROM quota_policies WHERE scope_type = 'global'")
            report["global_policy_count"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM quota_usage_ledger")
            report["quota_usage_ledger_count"] = cur.fetchone()[0]
    redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        report["redis_global_quota_present"] = bool(
            redis_client.exists("quota:config:global")
        )
    finally:
        redis_client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["missing_tables"] and report["global_policy_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
