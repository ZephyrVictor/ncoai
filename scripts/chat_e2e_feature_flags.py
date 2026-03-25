#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


ADMIN_BASE = env("ADMIN_BASE_URL", "http://127.0.0.1:3200/admin").rstrip("/")
CHAT_BASE = env("CHAT_BASE_URL", "http://127.0.0.1:3100/chat").rstrip("/")
ADMIN_USER = env("ADMIN_APP_USERNAME", "admin")
ADMIN_PASS = env("ADMIN_APP_PASSWORD", "admin123")
ARTIFACT_DIR = Path(env("ADMIN_E2E_ARTIFACT_DIR", ".local/acceptance"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_PATH = ARTIFACT_DIR / "chat-e2e-feature-flags.png"


def login_admin(page) -> None:
    page.goto(f"{ADMIN_BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.locator("#login-username").fill(ADMIN_USER)
    page.locator("#login-password").fill(ADMIN_PASS)
    page.locator("#login-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)


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


def create_user(page, identifier: str, password: str) -> None:
    page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
    page.locator("#user-identifier").fill(identifier)
    page.locator("#user-display-name").fill(identifier)
    page.locator("#user-password").fill(password)
    page.locator("#user-submit").click()
    page.wait_for_url("**/admin/users", timeout=30000)
    page.get_by_test_id(f"user-row-{identifier}").wait_for(timeout=30000)


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


def fetch_settings(page) -> dict:
    return page.evaluate(
        """
        async () => {
          const response = await fetch('/chat/project/settings', { credentials: 'include' });
          return response.json();
        }
        """
    )


def main() -> int:
    suffix = int(datetime.now().timestamp())
    username = f"feature-flags-{suffix}"
    password = f"feature-pass-{suffix}"
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

            settings_payload = fetch_settings(page)
            result["settings_features"] = settings_payload.get("features") or {}

            features = result["settings_features"]
            spontaneous_upload = ((features or {}).get("spontaneous_file_upload") or {}).get("enabled")
            audio_enabled = ((features or {}).get("audio") or {}).get("enabled")
            favorites_enabled = (features or {}).get("favorites")
            mcp_payload = (features or {}).get("mcp") or {}
            mcp_enabled = mcp_payload.get("enabled")
            allow_thread_sharing = (features or {}).get("allow_thread_sharing")

            if spontaneous_upload:
                raise RuntimeError("spontaneous file upload should be disabled")
            if audio_enabled:
                raise RuntimeError("audio should be disabled")
            if favorites_enabled:
                raise RuntimeError("favorites should be disabled")
            if mcp_enabled or any(
                (mcp_payload.get(key) or {}).get("enabled")
                for key in ("sse", "stdio", "streamable_http", "streamable-http")
            ):
                raise RuntimeError("MCP should be disabled")
            if allow_thread_sharing:
                raise RuntimeError("thread sharing should be disabled")

            page.locator("#chat-input").fill("feature flags check")
            page.locator("#chat-submit").click()
            page.locator('[data-step-type="assistant_message"]').last.wait_for(timeout=120000)

            result["upload_button_count"] = page.locator("#upload-button").count()
            result["upload_input_count"] = page.locator("#upload-button-input").count()
            result["favorite_button_count"] = page.locator(".favorite-popover-wrapper").count()
            result["mcp_dialog_count"] = page.locator("#mcp-servers").count()
            result["share_icon_count"] = page.locator("svg.lucide-share-2").count()

            if result["upload_button_count"] != 0 or result["upload_input_count"] != 0:
                raise RuntimeError("upload controls are still visible")
            if result["favorite_button_count"] != 0:
                raise RuntimeError("favorite controls are still visible")
            if result["mcp_dialog_count"] != 0:
                raise RuntimeError("MCP dialog is unexpectedly rendered")
            if result["share_icon_count"] != 0:
                raise RuntimeError("share controls are still visible")

            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            result["screenshot"] = str(SCREENSHOT_PATH)
        finally:
            browser.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
