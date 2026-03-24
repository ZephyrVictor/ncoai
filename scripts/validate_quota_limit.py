#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


BASE = env("QUOTA_LIMIT_BASE_URL", "http://127.0.0.1:3111/chat").rstrip("/")
USER = env("CHAINLIT_APP_USERNAME", "quota-limit")
PASS = env("CHAINLIT_APP_PASSWORD", "quota-limit-pass")
FIRST_MESSAGE = env("QUOTA_LIMIT_FIRST_MESSAGE", "Explain 42 in one short sentence.")
SECOND_MESSAGE = env("QUOTA_LIMIT_SECOND_MESSAGE", "Explain 43 in one short sentence.")
THIRD_MESSAGE = env("QUOTA_LIMIT_THIRD_MESSAGE", "Try one more short sentence.")
ARTIFACT_DIR = Path(env("QUOTA_ARTIFACT_DIR", ".local/acceptance"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_PATH = ARTIFACT_DIR / "quota-limit.png"


def wait_for_text(page, pattern: str, timeout_ms: int = 120000) -> str:
    locator = page.get_by_text(re.compile(pattern)).first
    locator.wait_for(timeout=timeout_ms)
    return locator.text_content() or ""


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

            chat_input = page.locator("#chat-input")
            submit_button = page.locator("#chat-submit")
            chat_input.wait_for(state="visible", timeout=30000)

            chat_input.fill(FIRST_MESSAGE)
            submit_button.click()
            page.get_by_role("article").filter(has_text=FIRST_MESSAGE).first.wait_for(
                timeout=30000
            )
            first_token_text = wait_for_text(
                page,
                r"本次\s+[\d,]+\s+tok\s+·\s+会话\s+[\d,]+\s*/\s*[\d,]+",
            )
            first_footer_text = wait_for_text(page, r"当前对话\s+[\d,]+\s*/\s*140\s+tok")

            chat_input.fill(SECOND_MESSAGE)
            submit_button.click()
            page.get_by_role("article").filter(has_text=SECOND_MESSAGE).first.wait_for(
                timeout=30000
            )

            blocked_text = wait_for_text(
                page,
                r"当前对话额度已用尽|用户总额度已用尽",
                timeout_ms=60000,
            )
            page.wait_for_timeout(2000)

            third_fill_error = None
            try:
                chat_input.fill(THIRD_MESSAGE, timeout=3000)
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                third_fill_error = str(exc)
            page.wait_for_timeout(1000)
            third_submit_disabled = submit_button.is_disabled()
            blocked_hint = None
            try:
                blocked_hint = page.get_by_text(
                    re.compile(r"当前对话额度已用尽|用户总额度已用尽")
                ).last.text_content(timeout=1000)
            except Exception:
                blocked_hint = None

            body_text = page.evaluate("document.body.innerText")
            disabled_attr = chat_input.get_attribute("disabled")
            aria_disabled = submit_button.get_attribute("aria-disabled")
            button_disabled = submit_button.is_disabled()

            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            print(
                json.dumps(
                    {
                        "base_url": BASE,
                        "first_message": FIRST_MESSAGE,
                        "second_message": SECOND_MESSAGE,
                        "third_message": THIRD_MESSAGE,
                        "first_token_text": first_token_text,
                        "first_footer_text": first_footer_text,
                        "blocked_text": blocked_text,
                        "chat_input_disabled_attr": disabled_attr,
                        "submit_button_aria_disabled": aria_disabled,
                        "submit_button_is_disabled": button_disabled,
                        "third_submit_is_disabled": third_submit_disabled,
                        "third_fill_error": third_fill_error,
                        "blocked_hint": blocked_hint,
                        "body_excerpt": body_text[:2000],
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
