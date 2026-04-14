from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page, async_playwright

from QPS_test.common import BrowserSmokeResult, ManagedUserCredential, RunConfig


THREAD_RE = re.compile(r"/thread/([^/?#]+)")


async def login_chat(
    page: Page,
    *,
    base_url: str,
    credential: ManagedUserCredential,
) -> None:
    await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_url("**/login", timeout=5000)
    except Exception:
        pass
    if "/login" in page.url or await page.locator("input#email").count():
        await page.fill("input#email", credential.identifier)
        await page.fill("input#password", credential.password)
        await page.get_by_role("button", name="Sign In").click()
    await page.wait_for_selector("#chat-input", timeout=30000)


async def send_prompt(page: Page, prompt: str) -> None:
    await page.locator("#chat-input").fill(prompt)
    await page.locator("#chat-submit").click()


async def wait_for_generation_complete(page: Page, timeout_ms: int = 90000) -> None:
    await page.locator("#stop-button").wait_for(state="visible", timeout=30000)
    await page.locator("#stop-button").wait_for(state="hidden", timeout=timeout_ms)
    await page.locator("#chat-input").wait_for(state="visible", timeout=15000)


def current_thread_id(page: Page) -> str | None:
    match = THREAD_RE.search(page.url)
    return match.group(1) if match else None


async def fetch_thread_ids(page: Page, *, chat_base_url: str) -> list[str]:
    payload = await page.evaluate(
        """
        async (chatBaseUrl) => {
          const response = await fetch(`${chatBaseUrl}/project/threads`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              pagination: { first: 20 },
              filter: {}
            })
          });
          const body = await response.json();
          return body?.data || [];
        }
        """,
        chat_base_url,
    )
    thread_ids: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("id"):
                thread_ids.append(str(item["id"]))
    return thread_ids


async def smoke_test(
    *,
    config: RunConfig,
    credential: ManagedUserCredential,
) -> BrowserSmokeResult:
    screenshots: list[str] = []
    notes: list[str] = []
    smoke_dir = config.paths.smoke_dir
    smoke_dir.mkdir(parents=True, exist_ok=True)
    login_ok = False
    stream_ok = False
    stop_ok = False
    thread_switch_ok = False
    smoke_timeout_ms = int(config.smoke_timeout_seconds * 1000)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        second_page: Page | None = None
        try:
            await login_chat(page, base_url=config.chat_base_url, credential=credential)
            login_ok = True

            await send_prompt(
                page,
                "Reply with exactly 20 short numbered lines about latency and throughput.",
            )
            await wait_for_generation_complete(page, timeout_ms=smoke_timeout_ms)
            thread_ids = await fetch_thread_ids(page, chat_base_url=config.chat_base_url)
            first_thread_id = thread_ids[0] if thread_ids else current_thread_id(page)
            if not first_thread_id:
                raise RuntimeError("failed to capture the first thread id")
            stream_ok = True
            first_shot = smoke_dir / "01-first-thread.png"
            await page.screenshot(path=str(first_shot), full_page=True)
            screenshots.append(first_shot.name)

            await send_prompt(
                page,
                "Reply with exactly 240 short numbered lines. Keep writing until stopped.",
            )
            await page.locator("#stop-button").wait_for(state="visible", timeout=30000)
            await page.wait_for_timeout(1500)
            await page.locator("#stop-button").click()
            await page.get_by_text("已停止").wait_for(timeout=5000)
            stop_ok = True
            stop_shot = smoke_dir / "02-stop.png"
            await page.screenshot(path=str(stop_shot), full_page=True)
            screenshots.append(stop_shot.name)

            second_page = await context.new_page()
            await login_chat(
                second_page,
                base_url=config.chat_base_url,
                credential=credential,
            )
            await send_prompt(
                second_page,
                "Reply with exactly 12 short numbered lines about socket sessions.",
            )
            await wait_for_generation_complete(second_page, timeout_ms=smoke_timeout_ms)
            second_thread_candidates = await fetch_thread_ids(
                second_page,
                chat_base_url=config.chat_base_url,
            )
            second_thread_id = next(
                (item for item in second_thread_candidates if item != first_thread_id),
                current_thread_id(second_page),
            )
            if not second_thread_id or second_thread_id == first_thread_id:
                raise RuntimeError("failed to create a second distinct thread")

            await page.goto(
                f"{config.chat_base_url}/thread/{second_thread_id}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_selector("#chat-input", timeout=30000)
            await send_prompt(
                page,
                "Reply with exactly 8 short numbered lines confirming thread switching works.",
            )
            await wait_for_generation_complete(page, timeout_ms=smoke_timeout_ms)
            thread_switch_ok = True
            switch_shot = smoke_dir / "03-thread-switch.png"
            await page.screenshot(path=str(switch_shot), full_page=True)
            screenshots.append(switch_shot.name)
            notes.append(
                f"thread switch verified: {first_thread_id} -> {second_thread_id}"
            )
        except Exception as exc:
            notes.append(str(exc))
        finally:
            if second_page is not None:
                await second_page.close()
            await page.close()
            await context.close()
            await browser.close()

    return BrowserSmokeResult(
        success=login_ok and stream_ok and stop_ok and thread_switch_ok,
        login_ok=login_ok,
        stream_ok=stream_ok,
        stop_ok=stop_ok,
        thread_switch_ok=thread_switch_ok,
        screenshots=screenshots,
        notes=notes,
    )
