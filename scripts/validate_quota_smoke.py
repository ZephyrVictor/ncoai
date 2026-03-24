#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import redis
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


BASE = env("QUOTA_BASE_URL", "http://127.0.0.1:3100/chat").rstrip("/")
DB = env("QUOTA_DATABASE_URL", "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai")
USER = env("CHAINLIT_APP_USERNAME", "acceptance")
PASS = env("CHAINLIT_APP_PASSWORD", "acceptance123")
MESSAGE = env("QUOTA_MESSAGE", f"quota smoke {int(time.time())}")
ARTIFACT_DIR = Path(env("QUOTA_ARTIFACT_DIR", ".local/acceptance"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_PATH = ARTIFACT_DIR / "quota-smoke.png"

REDIS_HOST = env("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(env("REDIS_PORT", "6379"))
REDIS_DB = int(env("REDIS_DB", "0"))

REDIS = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
)


def psql(query: str) -> str:
    proc = subprocess.run(
        ["psql", DB, "-Atq", "-F", "\t", "-c", query],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "psql failed")
    return proc.stdout.strip()


def wait_for_request_id(message: str, timeout_s: int = 120) -> tuple[str, str]:
    escaped = message.replace("'", "''")
    deadline = time.time() + timeout_s
    query = f"""
SELECT t.id, COALESCE(t.metadata::text, '{{}}')
FROM threads t
JOIN steps s ON s."threadId" = t.id
WHERE t."userIdentifier" = '{USER}'
  AND s.type = 'user_message'
  AND s.output = '{escaped}'
ORDER BY t."createdAt" DESC
LIMIT 1;
"""
    while time.time() < deadline:
        out = psql(query)
        if out:
            thread_id, metadata_raw = out.split("\t", 1)
            metadata = json.loads(metadata_raw or "{}")
            request_id = metadata.get("last_request_id")
            if request_id:
                return thread_id, request_id
        time.sleep(1)
    raise TimeoutError("request_id not found")


def wait_for_settlement(request_id: str, timeout_s: int = 180) -> dict:
    deadline = time.time() + timeout_s
    query = (
        "SELECT row_to_json(x)::text FROM ("
        "SELECT request_id, provider_request_id, thread_id, user_identifier, step_id, "
        "prompt_tokens, completion_tokens, total_tokens, reserved_tokens, status, usage_source "
        f"FROM quota_usage_ledger WHERE request_id = '{request_id}'"
        ") x;"
    )
    while time.time() < deadline:
        out = psql(query)
        if out:
            row = json.loads(out)
            if row.get("status") == "settled":
                return row
        time.sleep(1)
    raise TimeoutError("ledger settlement not found")


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_url("**/login", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            if "/login" in page.url or page.locator("input#email").count():
                page.fill("input#email", USER)
                page.fill("input#password", PASS)
                page.get_by_role("button", name="Sign In").click()
                page.wait_for_url("**/chat*", timeout=30000)

            page.locator("#chat-input").wait_for(state="visible", timeout=30000)
            page.locator("#chat-input").fill(MESSAGE)
            page.locator("#chat-submit").click()
            page.get_by_role("article").filter(has_text=MESSAGE).first.wait_for(
                timeout=30000
            )

            thread_id, request_id = wait_for_request_id(MESSAGE)
            ledger = wait_for_settlement(request_id)

            token_locator = page.get_by_text(
                re.compile(r"本次\s+[\d,]+\s+tok\s+·\s+会话\s+[\d,]+\s*/\s*[\d,]+")
            ).first
            token_locator.wait_for(timeout=120000)
            token_text = token_locator.text_content()

            footer_locator = page.get_by_text(
                re.compile(r"[\d,]+\s*/\s*8,192")
            ).first
            footer_locator.wait_for(timeout=30000)
            footer_text = footer_locator.text_content()

            avatar_text = page.locator("#user-nav-button").text_content(timeout=30000)
            quota_response = page.evaluate(
                """
                async () => {
                  const response = await fetch('/chat/api/quota/me', {
                    credentials: 'include'
                  });
                  return { status: response.status, body: await response.json() };
                }
                """
            )

            thread_summary = json.loads(
                psql(
                    "SELECT row_to_json(x)::text FROM ("
                    "SELECT thread_id, used_tokens, reserved_tokens, limit_tokens "
                    f"FROM quota_thread_summary WHERE thread_id = '{thread_id}'"
                    ") x;"
                )
            )
            user_summary = json.loads(
                psql(
                    "SELECT row_to_json(x)::text FROM ("
                    "SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens "
                    f"FROM quota_user_summary WHERE user_identifier = '{USER}'"
                    ") x;"
                )
            )
            thread_metadata = json.loads(
                psql(f"SELECT COALESCE(metadata::text, '{{}}') FROM threads WHERE id = '{thread_id}';")
            )

            redis_thread = REDIS.hgetall(f"quota:thread:{thread_id}")
            redis_user = REDIS.hgetall(f"quota:user:{USER}")
            redis_request = REDIS.hgetall(f"quota:req:{request_id}")

            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            print(
                json.dumps(
                    {
                        "message": MESSAGE,
                        "thread_id": thread_id,
                        "request_id": request_id,
                        "token_text": token_text,
                        "footer_text": footer_text,
                        "avatar_text": avatar_text,
                        "quota_response": quota_response,
                        "ledger": ledger,
                        "thread_summary": thread_summary,
                        "user_summary": user_summary,
                        "thread_metadata": thread_metadata,
                        "redis_thread": redis_thread,
                        "redis_user": redis_user,
                        "redis_request": redis_request,
                        "screenshot": str(SCREENSHOT_PATH),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
