#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import psycopg
    from psycopg.rows import dict_row
    from redis import Redis
except ModuleNotFoundError as exc:  # pragma: no cover - operational path
    raise SystemExit(
        "[ERROR] Missing runtime dependency "
        f"{exc.name!r}. Activate .venv or run bash scripts/deploy/init-host.sh first."
    )

CORE_TABLES = {
    "admin": [
        "admin_accounts",
        "managed_users",
        "quota_policies",
        "quota_usage_ledger",
        "quota_thread_summary",
        "quota_user_summary",
    ],
    "chainlit": [
        "users",
        "threads",
        "steps",
        "elements",
        "feedbacks",
        "admin_accounts",
        "managed_users",
        "quota_policies",
        "quota_usage_ledger",
        "quota_thread_summary",
        "quota_user_summary",
    ],
    "litellm": [],
}


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"[ERROR] Missing required environment variable: {name}")
    return value.strip()


def database_url_for_service(service: str) -> str:
    if service == "litellm":
        litellm_database_url = os.getenv("LITELLM_DATABASE_URL")
        if litellm_database_url:
            return litellm_database_url.strip()
        return env_required("DATABASE_URL")
    return env_required("DATABASE_URL")


def check_database(
    database_url: str,
    *,
    service: str,
) -> dict[str, object]:
    required_tables = CORE_TABLES.get(service, [])
    try:
        with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS current_database")
                database_name = cur.fetchone()["current_database"]

                report: dict[str, object] = {
                    "database": database_name,
                    "required_tables": required_tables,
                    "missing_tables": [],
                }

                if required_tables:
                    cur.execute(
                        """
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                        """
                    )
                    existing = {row["tablename"] for row in cur.fetchall()}
                    report["missing_tables"] = [
                        table for table in required_tables if table not in existing
                    ]

                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS global_policy_count
                        FROM quota_policies
                        WHERE scope_type = 'global'
                        """
                    )
                    report["global_policy_count"] = cur.fetchone()["global_policy_count"]

                    if report["missing_tables"]:
                        raise SystemExit(
                            "[ERROR] Database schema is incomplete. Missing tables: "
                            + ", ".join(report["missing_tables"])
                        )
                    if report["global_policy_count"] <= 0:
                        raise SystemExit(
                            "[ERROR] Global quota policy is missing. Run bash scripts/deploy/init-db.sh first."
                        )

                return report
    except psycopg.Error as exc:
        raise SystemExit(
            "[ERROR] Database is not ready. "
            "Start the PostgreSQL container and run bash scripts/deploy/init-db.sh first. "
            f"Details: {exc}"
        )


def check_redis(redis_url: str) -> dict[str, object]:
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        pong = client.ping()
        policy_present = bool(client.exists("quota:config:global"))
    except Exception as exc:
        raise SystemExit(
            "[ERROR] Redis is not ready. "
            "Start the Redis container before launching host services. "
            f"Details: {exc}"
        )
    finally:
        client.close()
    if not pong:
        raise SystemExit("[ERROR] Redis ping failed.")
    return {
        "ping": True,
        "global_quota_cache_present": policy_present,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate runtime prerequisites before starting a host service")
    parser.add_argument("--service", required=True, choices=sorted(CORE_TABLES))
    parser.add_argument("--require-db", action="store_true")
    parser.add_argument("--require-redis", action="store_true")
    args = parser.parse_args()

    report: dict[str, object] = {"service": args.service}

    if args.require_db:
        database_url = database_url_for_service(args.service)
        report["database"] = check_database(database_url, service=args.service)

    if args.require_redis:
        redis_url = env_required("REDIS_URL")
        report["redis"] = check_redis(redis_url)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
