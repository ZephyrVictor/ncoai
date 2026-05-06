#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - environment dependent fallback
    psycopg = None
    dict_row = None


THREADS_SQL = """
SELECT
    t."userIdentifier" AS user_identifier,
    t.id AS thread_id,
    t."createdAt" AS thread_created_at,
    t.name AS thread_name,
    COALESCE(
        NULLIF(u.metadata ->> 'display_name', ''),
        CASE
            WHEN split_part(t."userIdentifier", ':', 3) <> ''
            THEN 'Team #' || split_part(t."userIdentifier", ':', 3)
            ELSE t."userIdentifier"
        END
    ) AS display_name
FROM threads t
LEFT JOIN users u ON u.identifier = t."userIdentifier"
WHERE t."userIdentifier" LIKE %(scope_pattern)s
ORDER BY
    t."userIdentifier" ASC,
    COALESCE(
        NULLIF(t."createdAt", '')::timestamptz,
        to_timestamp(0)
    ) DESC,
    t.id ASC
"""


MESSAGES_SQL = """
SELECT
    t."userIdentifier" AS user_identifier,
    t.id AS thread_id,
    s.id AS step_id,
    s.type AS step_type,
    COALESCE(NULLIF(s.output, ''), NULLIF(s.input, ''), '') AS content,
    COALESCE(
        NULLIF(s."createdAt", ''),
        NULLIF(s.start, ''),
        NULLIF(s."end", ''),
        NULLIF(t."createdAt", ''),
        ''
    ) AS created_at
FROM threads t
JOIN steps s ON s."threadId" = t.id
WHERE t."userIdentifier" LIKE %(scope_pattern)s
  AND s.type IN ('user_message', 'assistant_message')
ORDER BY
    t.id ASC,
    COALESCE(
        NULLIF(s."createdAt", '')::timestamptz,
        NULLIF(s.start, '')::timestamptz,
        NULLIF(s."end", '')::timestamptz,
        NULLIF(t."createdAt", '')::timestamptz,
        to_timestamp(0)
    ) ASC,
    s.id ASC
"""


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def build_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgresql+asyncpg://"):
            return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return database_url
    return "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_database_url(database_url: str) -> str:
    if "@" not in database_url or "://" not in database_url:
        return database_url
    prefix, rest = database_url.split("://", 1)
    if "@" not in rest:
        return database_url
    credentials, tail = rest.split("@", 1)
    if ":" not in credentials:
        return f"{prefix}://***@{tail}"
    username = credentials.split(":", 1)[0]
    return f"{prefix}://{username}:***@{tail}"


@dataclass
class ExportPaths:
    output_dir: Path
    json_path: Path
    html_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export complete chat transcripts for gzctf-team:6:* and generate "
            "a self-contained HTML review page."
        )
    )
    parser.add_argument(
        "--database-url",
        default=build_database_url(),
        help="PostgreSQL connection URL. Defaults to DATABASE_URL or local dev database.",
    )
    parser.add_argument(
        "--scope-prefix",
        default="gzctf-team:6:",
        help="User identifier prefix to export. Defaults to gzctf-team:6:",
    )
    parser.add_argument(
        "--output-dir",
        default="inspect",
        help="Directory for generated review artifacts.",
    )
    parser.add_argument(
        "--json-name",
        default="gzctf-team-6-all-chat.json",
        help="Output JSON filename inside output-dir.",
    )
    parser.add_argument(
        "--html-name",
        default="gzctf-team-6-review.html",
        help="Output HTML filename inside output-dir.",
    )
    parser.add_argument(
        "--postgres-container",
        default=env("POSTGRES_CONTAINER", "ncoai-postgres"),
        help=(
            "Docker container name for PostgreSQL when psycopg is unavailable. "
            "Defaults to POSTGRES_CONTAINER or ncoai-postgres."
        ),
    )
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> ExportPaths:
    output_dir = Path(args.output_dir).resolve()
    return ExportPaths(
        output_dir=output_dir,
        json_path=output_dir / args.json_name,
        html_path=output_dir / args.html_name,
    )


def empty_session(thread_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session": {
            "thread_id": thread_row["thread_id"],
            "created_at": thread_row["thread_created_at"],
            "name": thread_row["thread_name"],
        },
        "stats": {
            "user_messages": 0,
            "assistant_messages": 0,
            "turns": 0,
            "is_balanced": True,
        },
        "turns": [],
    }


def start_turn(index: int, step_id: str, created_at: str, content: str) -> dict[str, Any]:
    return {
        "turn": index,
        "request_time": created_at,
        "user": content,
        "response_time": None,
        "llm_response": None,
        "user_step_id": step_id,
        "assistant_step_id": None,
    }


def attach_message(session: dict[str, Any], message_row: dict[str, Any]) -> None:
    stats = session["stats"]
    turns = session["turns"]
    step_type = message_row["step_type"]
    content = message_row["content"]
    created_at = message_row["created_at"]
    step_id = message_row["step_id"]

    if step_type == "user_message":
        stats["user_messages"] += 1
        turns.append(start_turn(len(turns) + 1, step_id, created_at, content))
        return

    stats["assistant_messages"] += 1
    if turns and turns[-1]["llm_response"] is None:
        turns[-1]["response_time"] = created_at
        turns[-1]["llm_response"] = content
        turns[-1]["assistant_step_id"] = step_id
        return

    turns.append(
        {
            "turn": len(turns) + 1,
            "request_time": None,
            "user": None,
            "response_time": created_at,
            "llm_response": content,
            "user_step_id": None,
            "assistant_step_id": step_id,
        }
    )


def finalize_session(session: dict[str, Any]) -> None:
    stats = session["stats"]
    stats["turns"] = len(session["turns"])
    stats["is_balanced"] = stats["user_messages"] == stats["assistant_messages"]


def sql_quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_db_identity(database_url: str) -> tuple[str, str]:
    parsed = urlparse(database_url)
    database_name = parsed.path.lstrip("/") or env("POSTGRES_DB", "ncoai_chat")
    username = parsed.username or env("POSTGRES_USER", "ncoai")
    return username, database_name


def fetch_snapshot_rows_via_psycopg(
    database_url: str,
    scope_pattern: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assert psycopg is not None
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute(THREADS_SQL, {"scope_pattern": scope_pattern})
                thread_rows = cur.fetchall()
                cur.execute(MESSAGES_SQL, {"scope_pattern": scope_pattern})
                message_rows = cur.fetchall()
    return thread_rows, message_rows


def fetch_snapshot_rows_via_docker_psql(
    database_url: str,
    scope_pattern: str,
    postgres_container: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    username, database_name = parse_db_identity(database_url)
    scope_literal = sql_quote_literal(scope_pattern)
    sql_text = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH threads_export AS (
{THREADS_SQL.replace("%(scope_pattern)s", scope_literal)}
),
messages_export AS (
{MESSAGES_SQL.replace("%(scope_pattern)s", scope_literal)}
)
SELECT json_build_object(
    'threads',
    COALESCE(
        (SELECT json_agg(row_to_json(t)) FROM threads_export t),
        '[]'::json
    ),
    'messages',
    COALESCE(
        (SELECT json_agg(row_to_json(m)) FROM messages_export m),
        '[]'::json
    )
);
COMMIT;
"""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            postgres_container,
            "psql",
            "-X",
            "-q",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            username,
            "-d",
            database_name,
        ],
        input=sql_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown psql error"
        raise RuntimeError(
            f"Failed to export snapshot via docker/psql from container {postgres_container}: {message}"
        )

    payload_text = result.stdout.strip()
    if not payload_text:
        raise RuntimeError("docker/psql snapshot export returned empty output")
    raw_payload = json.loads(payload_text)
    return raw_payload.get("threads", []), raw_payload.get("messages", [])


def fetch_snapshot_rows(
    database_url: str,
    scope_pattern: str,
    postgres_container: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if psycopg is not None:
        thread_rows, message_rows = fetch_snapshot_rows_via_psycopg(
            database_url=database_url,
            scope_pattern=scope_pattern,
        )
        return thread_rows, message_rows, "psycopg"

    thread_rows, message_rows = fetch_snapshot_rows_via_docker_psql(
        database_url=database_url,
        scope_pattern=scope_pattern,
        postgres_container=postgres_container,
    )
    return thread_rows, message_rows, "docker-psql"


def export_snapshot(
    database_url: str,
    scope_prefix: str,
    postgres_container: str,
) -> dict[str, Any]:
    scope_pattern = f"{scope_prefix}%"
    export_started_at = utc_now_iso()
    thread_rows, message_rows, query_backend = fetch_snapshot_rows(
        database_url=database_url,
        scope_pattern=scope_pattern,
        postgres_container=postgres_container,
    )

    users: list[dict[str, Any]] = []
    users_by_identifier: dict[str, dict[str, Any]] = {}
    sessions_by_thread_id: dict[str, dict[str, Any]] = {}

    for row in thread_rows:
        user_identifier = row["user_identifier"]
        user = users_by_identifier.get(user_identifier)
        if user is None:
            user = {
                "user_identifier": user_identifier,
                "display_name": row["display_name"],
                "stats": {
                    "sessions": 0,
                    "turns": 0,
                    "user_messages": 0,
                    "assistant_messages": 0,
                    "mismatched_sessions": 0,
                    "empty_sessions": 0,
                },
                "sessions": [],
            }
            users_by_identifier[user_identifier] = user
            users.append(user)

        session = empty_session(row)
        user["sessions"].append(session)
        sessions_by_thread_id[row["thread_id"]] = session

    for row in message_rows:
        session = sessions_by_thread_id.get(row["thread_id"])
        if session is None:
            continue
        attach_message(session, row)

    total_turns = 0
    total_empty_sessions = 0
    total_mismatched_sessions = 0

    for user in users:
        for session in user["sessions"]:
            finalize_session(session)
            stats = session["stats"]
            user["stats"]["turns"] += stats["turns"]
            user["stats"]["user_messages"] += stats["user_messages"]
            user["stats"]["assistant_messages"] += stats["assistant_messages"]
            user["stats"]["sessions"] += 1
            if stats["turns"] == 0:
                user["stats"]["empty_sessions"] += 1
                total_empty_sessions += 1
            if not stats["is_balanced"]:
                user["stats"]["mismatched_sessions"] += 1
                total_mismatched_sessions += 1
            total_turns += stats["turns"]

    users.sort(key=lambda item: item["user_identifier"])

    export_finished_at = utc_now_iso()
    payload = {
        "export_scope": f"{scope_prefix}*",
        "exported_at": export_finished_at,
        "export_started_at": export_started_at,
        "snapshot": {
            "database_url": redact_database_url(database_url),
            "isolation_level": "REPEATABLE READ READ ONLY",
            "query_backend": query_backend,
        },
        "summary": {
            "users": len(users),
            "sessions": len(thread_rows),
            "turns": total_turns,
            "raw_user_messages": sum(
                user["stats"]["user_messages"] for user in users
            ),
            "raw_assistant_messages": sum(
                user["stats"]["assistant_messages"] for user in users
            ),
            "mismatched_sessions": total_mismatched_sessions,
            "empty_sessions": total_empty_sessions,
        },
        "users": users,
    }
    return payload


def render_summary_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("Users", summary["users"]),
        ("Sessions", summary["sessions"]),
        ("Turns", summary["turns"]),
        ("User Msgs", summary["raw_user_messages"]),
        ("LLM Msgs", summary["raw_assistant_messages"]),
        ("Empty Sessions", summary["empty_sessions"]),
        ("Mismatched", summary["mismatched_sessions"]),
    ]
    return "".join(
        f"""
        <article class="summary-card">
          <div class="summary-label">{escape(label)}</div>
          <div class="summary-value">{value}</div>
        </article>
        """
        for label, value in cards
    )


def render_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    summary_cards = render_summary_cards(payload["summary"])
    exported_at = escape(str(payload["exported_at"]))
    export_scope = escape(str(payload["export_scope"]))
    snapshot_url = escape(str(payload["snapshot"]["database_url"]))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Team 6 Chat Review</title>
  <style>
    :root {{
      --bg: #eef3f8;
      --bg-deep: #d9e4f0;
      --panel: rgba(255, 255, 255, 0.9);
      --panel-strong: rgba(255, 255, 255, 0.98);
      --ink: #17212b;
      --muted: #617282;
      --line: rgba(23, 33, 43, 0.1);
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --accent-strong: #115e59;
      --user: #dceef8;
      --assistant: #ffffff;
      --warning: #b45309;
      --warning-soft: rgba(180, 83, 9, 0.12);
      --shadow: 0 24px 80px rgba(43, 56, 72, 0.14);
      --sidebar-width: 360px;
    }}

    * {{
      box-sizing: border-box;
    }}

    html, body {{
      margin: 0;
      height: 100%;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 30%),
        radial-gradient(circle at bottom right, rgba(59, 130, 246, 0.12), transparent 32%),
        linear-gradient(180deg, #f7fafc 0%, var(--bg) 50%, var(--bg-deep) 100%);
      font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
    }}

    body {{
      overflow: hidden;
    }}

    .app-shell {{
      height: 100%;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      gap: 16px;
      padding: 18px;
    }}

    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      padding: 22px 24px;
      background: linear-gradient(135deg, rgba(255,255,255,0.96), rgba(244, 249, 252, 0.92));
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }}

    .brand {{
      min-width: 0;
    }}

    .brand h1 {{
      margin: 0;
      font-size: clamp(28px, 3vw, 40px);
      line-height: 1.05;
      letter-spacing: -0.03em;
    }}

    .brand p {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
      max-width: 760px;
    }}

    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}

    .chip {{
      border: 1px solid var(--line);
      background: var(--panel-strong);
      border-radius: 999px;
      padding: 8px 12px;
    }}

    .top-controls {{
      display: flex;
      justify-content: flex-end;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      min-width: min(580px, 50vw);
    }}

    .control {{
      min-width: 190px;
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}

    .control.search {{
      flex: 1 1 260px;
    }}

    .control select,
    .control input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,0.92);
      padding: 12px 14px;
      color: var(--ink);
      font: inherit;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.65);
    }}

    .toggle {{
      margin-top: 20px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,0.78);
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}

    .summary-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
    }}

    .summary-card {{
      background: rgba(255,255,255,0.74);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 12px 28px rgba(43, 56, 72, 0.08);
      padding: 14px 16px;
      backdrop-filter: blur(8px);
    }}

    .summary-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}

    .summary-value {{
      margin-top: 8px;
      font-size: 26px;
      font-weight: 700;
    }}

    .workspace {{
      min-height: 0;
      display: grid;
      grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
      gap: 16px;
    }}

    .panel {{
      min-height: 0;
      display: flex;
      flex-direction: column;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      overflow: hidden;
      backdrop-filter: blur(12px);
    }}

    .sidebar {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }}

    .sidebar-header,
    .chat-header {{
      padding: 18px 18px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.8);
      backdrop-filter: blur(10px);
    }}

    .sidebar-header {{
      position: sticky;
      top: 0;
      z-index: 2;
    }}

    .panel-title,
    .chat-title {{
      margin: 0;
      font-size: 18px;
    }}

    .panel-subtitle {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}

    .session-scroll,
    .chat-scroll {{
      min-height: 0;
      overflow: auto;
    }}

    .session-scroll {{
      padding: 14px;
    }}

    .list {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}

    .session-item {{
      width: 100%;
      border: 1px solid transparent;
      border-radius: 18px;
      background: var(--panel-strong);
      color: inherit;
      cursor: pointer;
      text-align: left;
      padding: 14px;
      transition: transform 140ms ease, border-color 140ms ease, background 140ms ease, box-shadow 140ms ease;
    }}

    .session-item:hover {{
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.26);
      box-shadow: 0 12px 24px rgba(43, 56, 72, 0.08);
    }}

    .session-item.active {{
      border-color: rgba(15, 118, 110, 0.42);
      background: linear-gradient(180deg, rgba(15, 118, 110, 0.14), rgba(255,255,255,0.98));
      box-shadow: 0 18px 32px rgba(15, 118, 110, 0.12);
    }}

    .item-title {{
      font-size: 15px;
      font-weight: 700;
      line-height: 1.35;
    }}

    .item-meta {{
      margin-top: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }}

    .session-preview {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(31, 41, 51, 0.06);
    }}

    .badge.warning {{
      background: var(--warning-soft);
      color: var(--warning);
    }}

    .chat-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}

    .chat-kicker {{
      color: var(--accent-strong);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .ghost-button {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 10px 14px;
      background: rgba(255,255,255,0.92);
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      transition: transform 140ms ease, border-color 140ms ease;
    }}

    .ghost-button:hover {{
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.35);
    }}

    .chat-scroll {{
      padding: 18px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.46), rgba(247,250,252,0.74)),
        radial-gradient(circle at top, rgba(15, 118, 110, 0.05), transparent 25%);
      scroll-behavior: smooth;
    }}

    .chat-feed {{
      display: flex;
      flex-direction: column;
      gap: 18px;
      min-height: 100%;
    }}

    .session-meta-bar {{
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding-bottom: 8px;
      background: linear-gradient(180deg, rgba(247,250,252,0.95), rgba(247,250,252,0.7), transparent);
    }}

    .turn-block {{
      display: grid;
      gap: 12px;
    }}

    .turn-header {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .message-row {{
      display: flex;
    }}

    .message-row.user-row {{
      justify-content: flex-end;
    }}

    .message-row.assistant-row {{
      justify-content: flex-start;
    }}

    .message {{
      width: min(860px, 84%);
      display: grid;
      gap: 12px;
      border-radius: 22px;
      padding: 16px 18px;
      border: 1px solid rgba(23, 33, 43, 0.08);
      overflow: hidden;
      box-shadow: 0 18px 36px rgba(43, 56, 72, 0.08);
    }}

    .message.user {{
      background: linear-gradient(180deg, rgba(220, 238, 248, 0.96), rgba(255,255,255,0.88));
      border-top-right-radius: 10px;
    }}

    .message.assistant {{
      background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(246,249,252,0.92));
      border-top-left-radius: 10px;
    }}

    .message-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
    }}

    .message-time {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }}

    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: 14px/1.62 "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
    }}

    .empty-state {{
      height: 100%;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--muted);
      padding: 24px;
    }}

    .empty-state strong {{
      display: block;
      margin-bottom: 8px;
      font-size: 18px;
      color: var(--ink);
    }}

    @media (max-width: 1300px) {{
      .topbar {{
        flex-direction: column;
      }}

      .top-controls {{
        min-width: 100%;
        justify-content: flex-start;
      }}

      .workspace {{
        grid-template-columns: 320px minmax(0, 1fr);
      }}
    }}

    @media (max-width: 980px) {{
      body {{
        overflow: auto;
      }}

      .app-shell {{
        height: auto;
        min-height: 100%;
      }}

      .workspace {{
        grid-template-columns: 1fr;
      }}

      .sidebar {{
        min-height: 360px;
      }}

      .chat-scroll {{
        min-height: 60vh;
      }}

      .message {{
        width: min(100%, 860px);
      }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <h1>Team 6 Conversation Review</h1>
        <p>顶部右上角切换用户，左侧滚动浏览 session 历史，右侧像常规聊天界面一样查看完整消息流。切换 session 后会自动定位到最新回复。</p>
        <div class="hero-meta">
          <span class="chip">Scope: {export_scope}</span>
          <span class="chip">Exported At: {exported_at}</span>
          <span class="chip">Snapshot DB: {snapshot_url}</span>
        </div>
      </div>
      <div class="top-controls">
        <label class="control">
          <span>Review User</span>
          <select id="user-select"></select>
        </label>
        <label class="control search">
          <span>Find Session</span>
          <input id="session-search" type="search" placeholder="搜索 session 名称、thread id 或首条提问">
        </label>
        <label class="toggle">
          <input id="hide-empty" type="checkbox">
          隐藏空会话
        </label>
      </div>
    </header>

    <section class="summary-strip">
      {summary_cards}
    </section>

    <section class="workspace">
      <aside class="panel sidebar">
        <div class="sidebar-header">
          <h2 class="panel-title">Session History</h2>
          <p id="session-subtitle" class="panel-subtitle">先从右上角选择一个用户。</p>
        </div>
        <div class="session-scroll">
          <div id="session-list" class="list"></div>
        </div>
      </aside>

      <main class="panel">
        <div class="chat-header">
          <div>
            <div class="chat-kicker">Review Transcript</div>
            <h2 id="chat-title" class="chat-title">Select A Session</h2>
            <p id="transcript-subtitle" class="panel-subtitle">切换 session 后会自动滚动到最新消息。</p>
          </div>
          <button id="jump-latest" class="ghost-button" type="button">Jump To Latest</button>
        </div>
        <div id="transcript-panel" class="chat-scroll"></div>
      </main>
    </section>
  </div>

  <script id="export-data" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById('export-data').textContent);
    const state = {{
      users: payload.users || [],
      activeUser: null,
      activeSession: null,
      sessionQuery: '',
      hideEmpty: false
    }};

    const userSelect = document.getElementById('user-select');
    const sessionList = document.getElementById('session-list');
    const transcriptPanel = document.getElementById('transcript-panel');
    const sessionSubtitle = document.getElementById('session-subtitle');
    const transcriptSubtitle = document.getElementById('transcript-subtitle');
    const chatTitle = document.getElementById('chat-title');
    const sessionSearch = document.getElementById('session-search');
    const hideEmpty = document.getElementById('hide-empty');
    const jumpLatest = document.getElementById('jump-latest');

    function formatValue(value) {{
      if (value === null || value === undefined || value === '') {{
        return 'N/A';
      }}
      return String(value);
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }}

    function summarizeUser(user) {{
      const stats = user.stats || {{}};
      return `${{user.display_name || user.user_identifier}} · ${{formatValue(stats.sessions)}} sessions · ${{formatValue(stats.turns)}} turns`;
    }}

    function previewText(session) {{
      const firstTurn = (session.turns || [])[0];
      const raw = [
        firstTurn?.user,
        firstTurn?.llm_response,
        session.session?.name
      ].find((value) => value && String(value).trim());
      return raw ? String(raw).replace(/\\s+/g, ' ').trim() : 'No transcript preview for this session.';
    }}

    function filteredSessions(user) {{
      let sessions = user.sessions || [];
      if (state.hideEmpty) {{
        sessions = sessions.filter((session) => (session.stats?.turns || 0) > 0);
      }}
      const query = state.sessionQuery.trim().toLowerCase();
      if (!query) return sessions;
      return sessions.filter((session) => {{
        const haystack = [
          session.session?.name,
          session.session?.thread_id,
          previewText(session)
        ].join(' ').toLowerCase();
        return haystack.includes(query);
      }});
    }}

    function scrollTranscriptToLatest() {{
      transcriptPanel.scrollTop = transcriptPanel.scrollHeight;
    }}

    function setActiveUser(user) {{
      state.activeUser = user;
      const sessions = filteredSessions(user);
      state.activeSession = sessions[0] || null;
      render();
    }}

    function setActiveSession(session) {{
      state.activeSession = session;
      renderSessions();
      renderTranscript();
      requestAnimationFrame(() => {{
        scrollTranscriptToLatest();
      }});
    }}

    function renderUserSelect() {{
      const users = state.users;
      if (!users.length) {{
        userSelect.innerHTML = '<option value="">No users</option>';
        state.activeUser = null;
        return;
      }}

      if (!state.activeUser || !users.includes(state.activeUser)) {{
        state.activeUser = users[0];
      }}

      userSelect.innerHTML = users.map((user) => {{
        const stats = user.stats || {{}};
        const label = `${{user.display_name || user.user_identifier}} | ${{user.user_identifier}} | ${{formatValue(stats.sessions)}} sessions`;
        return `<option value="${{escapeHtml(user.user_identifier)}}">${{escapeHtml(label)}}</option>`;
      }}).join('');
      userSelect.value = state.activeUser.user_identifier;
    }}

    function ensureActiveSession(sessions) {{
      if (!sessions.length) {{
        state.activeSession = null;
        return;
      }}
      if (!state.activeSession || !sessions.includes(state.activeSession)) {{
        state.activeSession = sessions[0];
      }}
    }}

    function renderSessions() {{
      const user = state.activeUser;
      if (!user) {{
        sessionSubtitle.textContent = '先从右上角选择一个用户。';
        sessionList.innerHTML = '<div class="empty-state"><div><strong>暂无用户</strong>当前没有可审查的 Team 6 用户。</div></div>';
        return;
      }}

      const sessions = filteredSessions(user);
      sessionSubtitle.textContent = summarizeUser(user);

      if (!sessions.length) {{
        state.activeSession = null;
        sessionList.innerHTML = '<div class="empty-state"><div><strong>没有匹配 session</strong>可以取消隐藏空会话，或者换个关键词。</div></div>';
        return;
      }}

      ensureActiveSession(sessions);

      sessionList.innerHTML = sessions.map((session) => {{
        const active = session === state.activeSession ? 'active' : '';
        const meta = session.session || {{}};
        const stats = session.stats || {{}};
        const badges = [
          `<span class="badge">turns ${{formatValue(stats.turns)}}</span>`,
          `<span class="badge">user ${{formatValue(stats.user_messages)}}</span>`,
          `<span class="badge">llm ${{formatValue(stats.assistant_messages)}}</span>`
        ];
        if (!stats.is_balanced) {{
          badges.push('<span class="badge warning">unbalanced</span>');
        }}
        return `
          <button class="session-item ${{active}}" data-thread-id="${{escapeHtml(meta.thread_id || '')}}">
            <div class="item-title">${{escapeHtml(meta.name || '(untitled session)')}}</div>
            <div class="item-meta">
              <span class="badge">${{escapeHtml(meta.thread_id || '')}}</span>
              <span class="badge">${{escapeHtml(meta.created_at || 'N/A')}}</span>
            </div>
            <div class="item-meta">${{badges.join('')}}</div>
            <div class="session-preview">${{escapeHtml(previewText(session))}}</div>
          </button>
        `;
      }}).join('');

      const activeButton = sessionList.querySelector('button.active');
      if (activeButton) {{
        activeButton.scrollIntoView({{block: 'nearest'}});
      }}

      for (const button of sessionList.querySelectorAll('button[data-thread-id]')) {{
        button.addEventListener('click', () => {{
          const session = sessions.find((item) => item.session?.thread_id === button.dataset.threadId);
          if (session) setActiveSession(session);
        }});
      }}
    }}

    function renderTranscript() {{
      const session = state.activeSession;
      if (!session) {{
        chatTitle.textContent = 'Select A Session';
        transcriptSubtitle.textContent = '从左侧历史里选中一个 session 后，这里会以聊天形式展示完整对话。';
        transcriptPanel.innerHTML = '<div class="empty-state"><div><strong>暂无转录内容</strong>选中一个 session 后会在这里显示完整对话。</div></div>';
        return;
      }}

      const meta = session.session || {{}};
      const stats = session.stats || {{}};
      chatTitle.textContent = meta.name || '(untitled session)';
      transcriptSubtitle.textContent = `${{meta.thread_id || ''}} · ${{formatValue(stats.turns)}} turns · user ${{formatValue(stats.user_messages)}} · llm ${{formatValue(stats.assistant_messages)}}`;

      const turns = session.turns || [];
      if (!turns.length) {{
        transcriptPanel.innerHTML = `
          <div class="empty-state">
            <div>
              <strong>这个 session 没有 user / assistant 对话</strong>
              线程存在，但当前快照里没有 user_message / assistant_message。
            </div>
          </div>
        `;
        return;
      }}

      const turnsHtml = turns.map((turn) => `
        <section class="turn-block">
          <div class="turn-header">
            <strong>Turn ${{formatValue(turn.turn)}}</strong>
            <span class="badge">${{escapeHtml(turn.user_step_id || turn.assistant_step_id || 'no-step-id')}}</span>
          </div>
          <div class="message-row user-row">
            <article class="message user">
              <div class="message-label">User</div>
              <pre>${{escapeHtml(turn.user ?? '')}}</pre>
              <div class="message-time">Request Time: ${{escapeHtml(formatValue(turn.request_time))}}</div>
            </article>
          </div>
          <div class="message-row assistant-row">
            <article class="message assistant">
              <div class="message-label">LLM Response</div>
              <pre>${{escapeHtml(turn.llm_response ?? '')}}</pre>
              <div class="message-time">Response Time: ${{escapeHtml(formatValue(turn.response_time))}}</div>
            </article>
          </div>
        </section>
      `).join('');

      transcriptPanel.innerHTML = `
        <div class="chat-feed">
          <div class="session-meta-bar">
            <span class="badge">${{escapeHtml(meta.name || '(untitled session)')}}</span>
            <span class="badge">${{escapeHtml(meta.created_at || 'N/A')}}</span>
            <span class="badge">user ${{formatValue(stats.user_messages)}}</span>
            <span class="badge">llm ${{formatValue(stats.assistant_messages)}}</span>
          </div>
          ${{turnsHtml}}
        </div>
      `;
    }}

    function render() {{
      renderUserSelect();
      renderSessions();
      renderTranscript();
    }}

    userSelect.addEventListener('change', (event) => {{
      const nextUser = state.users.find((user) => user.user_identifier === event.target.value);
      if (nextUser) {{
        setActiveUser(nextUser);
      }}
    }});

    sessionSearch.addEventListener('input', (event) => {{
      state.sessionQuery = event.target.value || '';
      render();
    }});

    hideEmpty.addEventListener('change', (event) => {{
      state.hideEmpty = Boolean(event.target.checked);
      render();
    }});

    jumpLatest.addEventListener('click', () => {{
      scrollTranscriptToLatest();
    }});

    render();
    requestAnimationFrame(() => {{
      scrollTranscriptToLatest();
    }});
  </script>
</body>
</html>
"""


def write_outputs(payload: dict[str, Any], paths: ExportPaths) -> None:
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths.html_path.write_text(render_html(payload), encoding="utf-8")


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args)
    payload = export_snapshot(
        args.database_url,
        args.scope_prefix,
        args.postgres_container,
    )
    write_outputs(payload, paths)
    print(
        json.dumps(
            {
                "ok": True,
                "scope": payload["export_scope"],
                "summary": payload["summary"],
                "json_path": str(paths.json_path),
                "html_path": str(paths.html_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
