#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
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
SCREENSHOT_PATH = ARTIFACT_DIR / "admin-e2e-users.png"


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


def wait_for_quota_usage(user_identifier: str, timeout_s: int = 120) -> dict:
    deadline = time.time() + timeout_s
    query = (
        "SELECT row_to_json(x)::text FROM ("
        "SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens "
        f"FROM quota_user_summary WHERE user_identifier = '{user_identifier}'"
        ") x;"
    )
    while time.time() < deadline:
        out = psql(query)
        if out:
            row = json.loads(out)
            if int(row.get("used_tokens") or 0) > 0:
                return row
        time.sleep(1)
    raise TimeoutError("quota_user_summary not updated")


def latest_thread_id(user_identifier: str) -> str:
    query = (
        'SELECT id FROM threads '
        f'WHERE "userIdentifier" = \'{user_identifier}\' '
        'ORDER BY COALESCE(NULLIF("createdAt", \'\')::timestamptz, NOW()) DESC, id DESC LIMIT 1;'
    )
    value = psql(query)
    if not value:
        raise RuntimeError(f"No thread found for user {user_identifier}")
    return value


def wait_for_thread_deleted(thread_id: str, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = psql(
            f"SELECT COUNT(*) FROM threads WHERE id = '{thread_id}';"
        )
        if int(remaining or 0) == 0:
            return
        time.sleep(1)
    raise TimeoutError(f"Thread {thread_id} was not deleted in time")


def login_admin(page) -> None:
    page.goto(f"{ADMIN_BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.locator("#login-username").fill(ADMIN_USER)
    page.locator("#login-password").fill(ADMIN_PASS)
    page.locator("#login-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)


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


def failed_login_chat(page, username: str, password: str) -> None:
    page.goto(CHAT_BASE, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_url("**/login", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    page.fill("input#email", username)
    page.fill("input#password", password)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_timeout(3000)


def set_global_policy(
    page,
    *,
    total_limit: int,
    window_start: datetime,
    window_end: datetime,
) -> None:
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


def main() -> int:
    suffix = int(time.time())
    managed_user = f"admin-user-{suffix}"
    managed_pass = f"pass-{suffix}"
    first_message = f"admin user e2e {suffix}"
    second_message = f"disabled follow-up {suffix}"
    open_start = datetime.now() - timedelta(hours=1)
    open_end = datetime.now() + timedelta(days=2)

    result: dict[str, object] = {
        "managed_user": managed_user,
        "admin_base": ADMIN_BASE,
        "chat_base": CHAT_BASE,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        admin_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        chat_context = browser.new_context(viewport={"width": 1440, "height": 1100})
        chat_page = chat_context.new_page()
        login_retry_context = browser.new_context(viewport={"width": 1440, "height": 1100})
        login_retry_page = login_retry_context.new_page()
        try:
            login_admin(admin_page)
            set_global_policy(
                admin_page,
                total_limit=100000,
                window_start=open_start,
                window_end=open_end,
            )
            admin_page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
            admin_page.locator("#user-identifier").fill(managed_user)
            admin_page.locator("#user-display-name").fill(f"Managed {suffix}")
            admin_page.locator("#user-password").fill(managed_pass)
            admin_page.locator("#user-submit").click()
            admin_page.wait_for_url("**/admin/users", timeout=30000)
            admin_page.get_by_test_id(f"user-row-{managed_user}").wait_for(timeout=30000)
            row_text = admin_page.get_by_test_id(f"user-row-{managed_user}").text_content()
            result["user_row_after_create"] = row_text

            login_chat(chat_page, managed_user, managed_pass)
            chat_page.locator("#chat-input").fill(first_message)
            chat_page.locator("#chat-submit").click()
            chat_page.get_by_role("article").filter(has_text=first_message).first.wait_for(
                timeout=30000
            )
            token_text = chat_page.get_by_text(
                re.compile(r"本次\s+[\d,]+\s+tok\s+·\s+会话\s+[\d,]+\s*/\s*[\d,]+")
            ).first.text_content(timeout=120000)
            result["first_token_text"] = token_text

            usage_row = wait_for_quota_usage(managed_user)
            result["quota_usage_row"] = usage_row
            thread_id = latest_thread_id(managed_user)
            result["latest_thread_id"] = thread_id

            delete_result = chat_page.evaluate(
                """
                async (threadId) => {
                  const response = await fetch('/chat/project/thread', {
                    method: 'DELETE',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ threadId })
                  });
                  return {
                    status: response.status,
                    body: await response.json()
                  };
                }
                """,
                thread_id,
            )
            result["delete_thread_response"] = delete_result
            if delete_result.get("status") != 200:
                raise RuntimeError(f"Failed to delete thread: {delete_result}")
            wait_for_thread_deleted(thread_id)

            usage_after_delete = wait_for_quota_usage(managed_user)
            result["quota_usage_after_thread_delete"] = usage_after_delete
            if int(usage_after_delete.get("used_tokens") or 0) != int(
                usage_row.get("used_tokens") or 0
            ):
                raise RuntimeError("User token usage changed after deleting the thread")

            admin_page.reload(wait_until="domcontentloaded", timeout=30000)
            admin_page.get_by_test_id(f"user-row-{managed_user}").wait_for(timeout=30000)
            updated_row_text = admin_page.get_by_test_id(f"user-row-{managed_user}").text_content()
            result["user_row_after_chat"] = updated_row_text

            admin_page.goto(f"{ADMIN_BASE}/users?edit={managed_user}", wait_until="domcontentloaded", timeout=30000)
            active_checkbox = admin_page.locator("#user-is-active")
            if active_checkbox.is_checked():
                active_checkbox.uncheck()
            admin_page.locator("#user-submit").click()
            admin_page.wait_for_url("**/admin/users", timeout=30000)
            disabled_row_text = admin_page.get_by_test_id(f"user-row-{managed_user}").text_content(timeout=30000)
            result["user_row_after_disable"] = disabled_row_text

            chat_page.reload(wait_until="domcontentloaded", timeout=30000)
            chat_page.locator("#chat-input").fill(second_message)
            chat_page.locator("#chat-submit").click()
            disabled_message = chat_page.get_by_text("当前用户已被禁用，请联系管理员。").last
            disabled_message.wait_for(timeout=30000)
            result["disabled_message"] = disabled_message.text_content()

            admin_page.once("dialog", lambda dialog: dialog.accept())
            admin_page.get_by_test_id(f"user-row-{managed_user}").locator(
                "[data-delete-user]"
            ).click()
            admin_page.wait_for_load_state("domcontentloaded", timeout=30000)
            admin_page.wait_for_timeout(1000)
            if admin_page.get_by_test_id(f"user-row-{managed_user}").count():
                raise RuntimeError("Deleted user row still visible after reload")

            user_db_row = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT identifier, is_active, is_deleted, deleted_at FROM managed_users "
                f"WHERE identifier = '{managed_user}'"
                ") x;"
            )
            thread_count = psql(
                f"SELECT COUNT(*) FROM threads WHERE \"userIdentifier\" = '{managed_user}';"
            )
            ledger_count = psql(
                f"SELECT COUNT(*) FROM quota_usage_ledger WHERE user_identifier = '{managed_user}';"
            )
            result["managed_user_db"] = json.loads(user_db_row)
            result["thread_count"] = int(thread_count or 0)
            result["ledger_count"] = int(ledger_count or 0)

            failed_login_chat(login_retry_page, managed_user, managed_pass)
            result["login_after_delete_url"] = login_retry_page.url
            result["login_after_delete_still_on_login"] = "/login" in login_retry_page.url

            admin_page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            result["screenshot"] = str(SCREENSHOT_PATH)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            login_retry_context.close()
            chat_context.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
