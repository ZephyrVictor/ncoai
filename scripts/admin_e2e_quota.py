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
SCREENSHOT_PATH = ARTIFACT_DIR / "admin-e2e-quota.png"


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


def quota_fetch(page) -> dict:
    return page.evaluate(
        """
        async () => {
          const response = await fetch('/chat/api/quota/me', { credentials: 'include' });
          return { status: response.status, body: await response.json() };
        }
        """
    )


def create_user(page, identifier: str, password: str) -> str:
    page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
    page.locator("#user-identifier").fill(identifier)
    page.locator("#user-display-name").fill(identifier)
    page.locator("#user-password").fill(password)
    page.locator("#user-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)
    row = page.get_by_test_id(f"user-row-{identifier}")
    row.wait_for(timeout=30000)
    return row.text_content() or ""


def set_global_policy(
    page,
    *,
    total_limit: int,
    window_start: datetime,
    window_end: datetime,
    is_active: bool = True,
) -> dict[str, str]:
    page.goto(f"{ADMIN_BASE}/quota-policies", wait_until="domcontentloaded", timeout=30000)
    page.locator("#policy-limit").fill(str(total_limit))
    page.locator("#policy-window-start").fill(window_start.strftime("%Y-%m-%dT%H:%M"))
    page.locator("#policy-window-end").fill(window_end.strftime("%Y-%m-%dT%H:%M"))
    checkbox = page.locator("#policy-is-active")
    if checkbox.is_checked() != is_active:
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
    return {
        "summary": page.get_by_test_id("policy-current-summary").text_content(timeout=30000)
        or "",
        "flash": page.get_by_test_id("flash-banner").text_content(timeout=30000) or "",
    }


def wait_for_input_state(
    page,
    *,
    input_disabled: bool,
    submit_disabled: bool,
    timeout_ms: int = 30000,
) -> None:
    page.wait_for_function(
        """
        ([expectedInputDisabled, expectedSubmitDisabled]) => {
          const input = document.querySelector('#chat-input');
          const submit = document.querySelector('#chat-submit');
          if (!input || !submit) return false;
          return input.disabled === expectedInputDisabled &&
            submit.disabled === expectedSubmitDisabled;
        }
        """,
        arg=[input_disabled, submit_disabled],
        timeout=timeout_ms,
    )


def user_nav_quota_text(page) -> str:
    text = page.locator("#user-nav-button").text_content(timeout=30000) or ""
    match = re.search(r"[\d,]+\s*/\s*[\d,]+", text)
    if not match:
        raise RuntimeError(f"Failed to find user nav quota text in: {text!r}")
    return match.group(0)


def wait_for_user_nav_quota(page, expected: str, timeout_ms: int = 20000) -> None:
    page.wait_for_function(
        """
        (expectedValue) => {
          const node = document.querySelector('#user-nav-button');
          return !!node && node.textContent.includes(expectedValue);
        }
        """,
        arg=expected,
        timeout=timeout_ms,
    )


def wait_for_user_nav_limit(page, limit_tokens: int, timeout_ms: int = 20000) -> None:
    expected = f"/ {limit_tokens:,}"
    page.wait_for_function(
        """
        (expectedValue) => {
          const node = document.querySelector('#user-nav-button');
          return !!node && node.textContent.includes(expectedValue);
        }
        """,
        arg=expected,
        timeout=timeout_ms,
    )


def wait_for_blocked(page, timeout_ms: int = 90000) -> str:
    locator = page.get_by_text(
        re.compile(r"当前对话额度已用尽|用户总额度已用尽|当前不在配额生效时间内|当前没有可用配额配置")
    ).last
    locator.wait_for(timeout=timeout_ms)
    return locator.text_content() or ""


def chat_footer_text(page, timeout_ms: int = 30000) -> str:
    return (
        page.get_by_text(re.compile(r"当前对话\s+[\d,]+\s*/\s*[\d,]+\s*tok"))
        .last.text_content(timeout=timeout_ms)
        or ""
    )


def wait_for_quota(
    page,
    *,
    expected_status: str | None = None,
    expected_limit: int | None = None,
    min_used_tokens: int | None = None,
    timeout_ms: int = 20000,
) -> dict:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        payload = quota_fetch(page)
        body = payload.get("body", {})
        quota = body.get("quota") or {}
        status_ok = expected_status is None or quota.get("status") == expected_status
        limit_ok = expected_limit is None or int(quota.get("limit_tokens") or 0) == expected_limit
        used_ok = min_used_tokens is None or int(quota.get("used_tokens") or 0) >= min_used_tokens
        if status_ok and limit_ok and used_ok:
            return payload
        time.sleep(1)
    raise TimeoutError(
        f"quota did not reach expected state status={expected_status!r} limit={expected_limit!r}"
    )


def main() -> int:
    suffix = int(time.time())
    managed_user = f"quota-user-{suffix}"
    managed_pass = f"quota-pass-{suffix}"
    open_start = datetime.now() - timedelta(hours=1)
    open_end = datetime.now() + timedelta(days=2)
    closed_start = datetime.now() - timedelta(days=2)
    closed_end = datetime.now() - timedelta(minutes=1)

    result: dict[str, object] = {
        "managed_user": managed_user,
        "admin_base": ADMIN_BASE,
        "chat_base": CHAT_BASE,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        admin_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        chat_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login_admin(admin_page)
            result["global_policy_open"] = set_global_policy(
                admin_page,
                total_limit=400,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )
            result["user_row_after_create"] = create_user(admin_page, managed_user, managed_pass)

            login_chat(chat_page, managed_user, managed_pass)
            chat_page.bring_to_front()
            wait_for_user_nav_limit(chat_page, 400, timeout_ms=20000)
            initial_quota = wait_for_quota(
                chat_page,
                expected_status="ok",
                expected_limit=400,
                timeout_ms=20000,
            )
            result["initial_quota"] = initial_quota
            result["initial_user_nav_quota"] = user_nav_quota_text(chat_page)

            first_message = (
                "Give me six short bullet points describing the number seven. Keep it concise."
            )
            chat_page.locator("#chat-input").fill(first_message)
            chat_page.locator("#chat-submit").click()
            chat_page.get_by_role("article").filter(has_text=first_message).first.wait_for(
                timeout=30000
            )
            first_reply_quota = wait_for_quota(
                chat_page,
                expected_status="ok",
                expected_limit=400,
                min_used_tokens=1,
                timeout_ms=120000,
            )
            result["quota_after_first_reply"] = first_reply_quota
            used_after_first_reply = int(
                ((first_reply_quota.get("body") or {}).get("quota") or {}).get("used_tokens") or 0
            )
            if used_after_first_reply <= 1:
                raise RuntimeError("First reply did not produce any token usage")
            result["used_after_first_reply"] = used_after_first_reply

            lowered_limit = max(used_after_first_reply - 1, 1)
            result["lowered_limit"] = lowered_limit
            result["global_policy_lowered"] = set_global_policy(
                admin_page,
                total_limit=lowered_limit,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )
            chat_page.bring_to_front()
            wait_for_user_nav_limit(chat_page, lowered_limit, timeout_ms=20000)
            blocked_quota = wait_for_quota(
                chat_page,
                expected_status="blocked",
                expected_limit=lowered_limit,
                timeout_ms=30000,
            )
            result["blocked_quota"] = blocked_quota
            result["blocked_user_nav_quota"] = user_nav_quota_text(chat_page)
            blocked_text = wait_for_blocked(chat_page, timeout_ms=30000)
            result["blocked_text"] = blocked_text
            wait_for_input_state(
                chat_page,
                input_disabled=True,
                submit_disabled=True,
                timeout_ms=30000,
            )
            result["input_disabled_after_block"] = chat_page.locator("#chat-input").is_disabled()
            result["submit_disabled_after_block"] = chat_page.locator("#chat-submit").is_disabled()

            raised_limit = used_after_first_reply + 1000
            result["raised_limit"] = raised_limit
            result["global_policy_raised"] = set_global_policy(
                admin_page,
                total_limit=raised_limit,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )
            chat_page.bring_to_front()
            wait_for_user_nav_limit(chat_page, raised_limit, timeout_ms=20000)
            raised_quota = wait_for_quota(
                chat_page,
                expected_status="ok",
                expected_limit=raised_limit,
                timeout_ms=30000,
            )
            result["raised_quota"] = raised_quota
            wait_for_input_state(
                chat_page,
                input_disabled=False,
                submit_disabled=True,
                timeout_ms=30000,
            )
            result["input_disabled_after_raise"] = chat_page.locator("#chat-input").is_disabled()
            result["submit_disabled_after_raise"] = chat_page.locator("#chat-submit").is_disabled()
            result["raised_user_nav_quota"] = user_nav_quota_text(chat_page)

            recovery_message = f"quota recovery {suffix}"
            chat_page.locator("#chat-input").fill(recovery_message)
            chat_page.locator("#chat-submit").click()
            chat_page.get_by_role("article").filter(has_text=recovery_message).first.wait_for(
                timeout=30000
            )
            quota_after_recovery = wait_for_quota(
                chat_page,
                expected_status="ok",
                expected_limit=raised_limit,
                min_used_tokens=used_after_first_reply + 1,
                timeout_ms=30000,
            )
            result["quota_after_recovery"] = quota_after_recovery
            used_after_recovery = int(
                ((quota_after_recovery.get("body") or {}).get("quota") or {}).get("used_tokens") or 0
            )
            result["recovery_token_text"] = (
                chat_page.get_by_text(
                    re.compile(
                        rf"本次\s+[\d,]+\s+tok\s+·\s+会话\s+{used_after_recovery:,}\s*/\s*8,192"
                    )
                )
                .last.text_content(timeout=120000)
                or ""
            )
            wait_for_user_nav_quota(
                chat_page,
                f"{used_after_recovery:,} / {raised_limit:,}",
                timeout_ms=30000,
            )
            result["footer_text_after_recovery"] = chat_footer_text(chat_page)
            result["user_nav_quota_after_recovery"] = user_nav_quota_text(chat_page)

            result["global_policy_closed"] = set_global_policy(
                admin_page,
                total_limit=raised_limit,
                window_start=closed_start,
                window_end=closed_end,
                is_active=True,
            )
            chat_page.bring_to_front()
            window_closed_quota = wait_for_quota(
                chat_page,
                expected_status="window_closed",
                expected_limit=raised_limit,
                timeout_ms=30000,
            )
            result["window_closed_quota"] = window_closed_quota
            try:
                wait_for_input_state(
                    chat_page,
                    input_disabled=True,
                    submit_disabled=True,
                    timeout_ms=20000,
                )
            except PlaywrightTimeoutError:
                chat_page.reload(wait_until="domcontentloaded", timeout=30000)
                chat_page.locator("#chat-input").wait_for(state="visible", timeout=30000)
                wait_for_input_state(
                    chat_page,
                    input_disabled=True,
                    submit_disabled=True,
                    timeout_ms=30000,
                )
            result["window_closed_text"] = wait_for_blocked(chat_page, timeout_ms=20000)
            result["input_disabled_after_window_closed"] = chat_page.locator("#chat-input").is_disabled()
            result["submit_disabled_after_window_closed"] = chat_page.locator("#chat-submit").is_disabled()

            result["global_policy_restored"] = set_global_policy(
                admin_page,
                total_limit=400,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )

            admin_page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
            admin_page.get_by_test_id(f"user-row-{managed_user}").wait_for(timeout=30000)
            result["user_row_final"] = admin_page.get_by_test_id(f"user-row-{managed_user}").text_content()

            summary = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens, window_start, window_end "
                f"FROM quota_user_summary WHERE user_identifier = '{managed_user}'"
                ") x;"
            )
            result["quota_user_summary"] = json.loads(summary)

            chat_page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            result["screenshot"] = str(SCREENSHOT_PATH)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
