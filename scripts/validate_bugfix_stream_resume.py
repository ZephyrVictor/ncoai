#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3100/chat"
USER = "acceptance"
PASSWORD = "acceptance123"
DB = "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat"


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


def login(page) -> None:
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    if page.locator("input#email").count():
        page.fill("input#email", USER)
        page.fill("input#password", PASSWORD)
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_url("**/chat*", timeout=30000)


def main() -> int:
    marker = f"stream-resume-{int(time.time())}"
    prompt = (
        f"{marker} output a numbered list from 1 to 1000. "
        "Each line must use the exact format "
        "<number>. alpha beta gamma delta epsilon zeta eta theta iota kappa. "
        "Do not add any explanation before or after the list."
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login(page)

            page.locator("#chat-input").fill(prompt)
            page.locator("#chat-submit").click()
            page.get_by_text(marker).wait_for(timeout=30000)

            escaped_prompt = prompt.replace("'", "''")
            thread_id = ""
            deadline = time.time() + 60
            while time.time() < deadline:
                query = f"""
SELECT t.id::text
FROM threads t
JOIN steps s ON s."threadId" = t.id
WHERE t."userIdentifier" = '{USER}'
  AND s.type = 'user_message'
  AND s.output = '{escaped_prompt}'
ORDER BY t."createdAt"::timestamptz DESC
LIMIT 1;
"""
                thread_id = psql(query)
                if thread_id:
                    break
                time.sleep(1)
            if not thread_id:
                raise RuntimeError("thread id not found")

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

            page.goto(f"{BASE}/thread/{thread_id}", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

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

            db_len = int(
                psql(
                    f"""
SELECT COALESCE(length(output), 0)
FROM steps
WHERE "threadId" = '{thread_id}'::uuid
  AND type = 'assistant_message'
ORDER BY "createdAt"::timestamptz DESC
LIMIT 1;
"""
                )
            )

            print(
                json.dumps(
                    {
                        "thread_id": thread_id,
                        "initial_len_before_resume": initial_len,
                        "visible_len_immediately_after_resume": resumed_initial,
                        "max_visible_len_after_resume": grown_len,
                        "db_output_len": db_len,
                        "resume_url": page.url,
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
