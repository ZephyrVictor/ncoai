#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


ADMIN_BASE = env("ADMIN_BASE_URL", "http://127.0.0.1:3200/admin").rstrip("/")
CHAT_BASE = env("CHAT_BASE_URL", "http://127.0.0.1:3100/chat").rstrip("/")
DB = env(
    "ADMIN_E2E_DATABASE_URL",
    env("DATABASE_URL", "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat"),
)
ADMIN_USER = env("ADMIN_APP_USERNAME", "admin")
ADMIN_PASS = env("ADMIN_APP_PASSWORD", "admin123")
ARTIFACT_DIR = Path(env("ADMIN_E2E_ARTIFACT_DIR", ".local/acceptance"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_PATH = ARTIFACT_DIR / "chat-e2e-stream-resume-copy.png"


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


def login_admin(page) -> None:
    page.goto(f"{ADMIN_BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.locator("#login-username").fill(ADMIN_USER)
    page.locator("#login-password").fill(ADMIN_PASS)
    page.locator("#login-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)


def create_user(page, identifier: str, password: str) -> None:
    page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
    page.locator("#user-identifier").fill(identifier)
    page.locator("#user-display-name").fill(identifier)
    page.locator("#user-password").fill(password)
    page.locator("#user-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)
    page.get_by_test_id(f"user-row-{identifier}").wait_for(timeout=30000)


def set_global_policy(page, *, total_limit: int, window_start: datetime, window_end: datetime) -> None:
    page.goto(f"{ADMIN_BASE}/quota-policies", wait_until="domcontentloaded", timeout=30000)
    page.locator("#policy-limit").fill(str(total_limit))
    page.locator("#policy-window-start").fill(window_start.strftime("%Y-%m-%dT%H:%M"))
    page.locator("#policy-window-end").fill(window_end.strftime("%Y-%m-%dT%H:%M"))
    checkbox = page.locator("#policy-is-active")
    if not checkbox.is_checked():
        checkbox.click()
    page.locator("#policy-submit").click()
    page.wait_for_function(
        """
        () => {
          const flash = document.querySelector('[data-testid="flash-banner"]');
          return !!flash?.textContent?.includes('全局配额已更新');
        }
        """,
        timeout=30000,
    )


def login_chat(page, username: str, password: str) -> None:
    page.goto(CHAT_BASE, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_url("**/login", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    if "/login" in page.url or page.locator("input#email").count():
        page.fill("input#email", username)
        page.fill("input#password", password)
        page.get_by_role("button", name="Sign In").click()
    page.wait_for_url("**/chat*", timeout=30000)
    page.locator("#chat-input").wait_for(state="visible", timeout=30000)


def latest_thread_id(user_identifier: str, prompt: str) -> str:
    escaped_user = user_identifier.replace("'", "''")
    escaped_prompt = prompt.replace("'", "''")
    query = f"""
SELECT t.id::text
FROM threads t
JOIN steps s ON s."threadId" = t.id
WHERE t."userIdentifier" = '{escaped_user}'
  AND s.type = 'user_message'
  AND s.output = '{escaped_prompt}'
ORDER BY COALESCE(NULLIF(t."createdAt", '')::timestamptz, NOW()) DESC
LIMIT 1;
"""
    deadline = time.time() + 90
    while time.time() < deadline:
        value = psql(query)
        if value:
            return value
        time.sleep(1)
    raise TimeoutError("thread id not found")


def latest_output_length(thread_id: str) -> int:
    return int(
        psql(
            f"""
SELECT COALESCE(length(output), 0)
FROM steps
WHERE "threadId" = '{thread_id}'::uuid
  AND type = 'assistant_message'
ORDER BY COALESCE(NULLIF("createdAt", '')::timestamptz, NOW()) DESC
LIMIT 1;
"""
        )
        or "0"
    )


def main() -> int:
    suffix = int(time.time())
    username = f"stream-copy-{suffix}"
    password = f"stream-pass-{suffix}"
    marker = f"stream-copy-{suffix}"
    prompt = (
        f"{marker} output a numbered list from 1 to 1000. "
        "Each line must use the exact format "
        "<number>. alpha beta gamma delta epsilon zeta eta theta iota kappa. "
        "Do not add any explanation before or after the list."
    )
    open_start = datetime.now() - timedelta(hours=1)
    open_end = datetime.now() + timedelta(days=2)

    result: dict[str, object] = {
        "username": username,
        "admin_base": ADMIN_BASE,
        "chat_base": CHAT_BASE,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        admin_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login_admin(admin_page)
            set_global_policy(
                admin_page,
                total_limit=100000,
                window_start=open_start,
                window_end=open_end,
            )
            create_user(admin_page, username, password)
            login_chat(page, username, password)

            page.locator("#chat-input").fill(prompt)
            page.locator("#chat-submit").click()
            page.get_by_text(marker).wait_for(timeout=30000)
            thread_id = latest_thread_id(username, prompt)
            result["thread_id"] = thread_id

            assistant = page.locator(
                '[data-step-type="assistant_message"] .message-content'
            ).last
            initial_len = 0
            deadline = time.time() + 90
            while time.time() < deadline:
                text = assistant.inner_text(timeout=5000).strip()
                if len(text) > 80:
                    initial_len = len(text)
                    break
                page.wait_for_timeout(500)
            if initial_len <= 80:
                raise RuntimeError("assistant content did not start streaming")

            page.goto(f"{CHAT_BASE}/thread/{thread_id}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            resumed = page.locator(
                '[data-step-type="assistant_message"] .message-content'
            ).last
            resumed_initial = len(resumed.inner_text(timeout=5000).strip())

            grown_len = resumed_initial
            deadline = time.time() + 120
            while time.time() < deadline:
                text = resumed.inner_text(timeout=5000).strip()
                grown_len = max(grown_len, len(text))
                if grown_len > initial_len + 120:
                    break
                page.wait_for_timeout(1000)

            copy_button = page.locator('[data-step-type="assistant_message"]').last.locator("button").first
            copy_button.wait_for(state="visible", timeout=120000)
            copy_button.click()
            copy_button.locator("svg.lucide-check").wait_for(timeout=5000)

            db_len = latest_output_length(thread_id)
            result.update(
                {
                    "initial_len_before_resume": initial_len,
                    "visible_len_immediately_after_resume": resumed_initial,
                    "max_visible_len_after_resume": grown_len,
                    "db_output_len": db_len,
                    "copy_button_confirmed": True,
                    "resume_url": page.url,
                }
            )
            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        finally:
            browser.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
