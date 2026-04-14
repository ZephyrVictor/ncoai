from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.async_api import Page, async_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIR = REPO_ROOT / "apps/chainlit/vendor/chainlit/frontend"
VITE_URL = "http://127.0.0.1:4173/__e2e__/chat-lifecycle"
VITE_HEALTH_URL = "http://127.0.0.1:4173/"
VITE_LOG = REPO_ROOT / ".tmp-chat-lifecycle-vite.log"
FULL_REPLY = "这是一条用于验证pending切换与停止生成行为的模拟回复。"


def wait_for_server(url: str, timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    while time.time() < deadline:
        try:
            with opener.open(url) as response:
                if response.status < 500:
                    return
        except urllib.error.URLError as exc:
            last_error = exc
        time.sleep(0.5)

    log_tail = ""
    if VITE_LOG.exists():
        log_tail = VITE_LOG.read_text()[-4000:]
    raise RuntimeError(
        f"Timed out waiting for Vite dev server at {url}. "
        f"Last error: {last_error}\n\nRecent log tail:\n{log_tail}"
    )


async def snapshot(page: Page) -> dict:
    data = await page.evaluate(
        "() => window.__CHAINLIT_E2E__ ? window.__CHAINLIT_E2E__.snapshot() : null"
    )
    if not data:
        raise AssertionError("E2E harness snapshot API is not available")
    return data


def assistant_message(data: dict) -> dict:
    messages = data.get("messages", [])
    for message in reversed(messages):
        if message.get("type") == "assistant_message":
            return message
    raise AssertionError("Assistant message was not found in snapshot")


async def verify_pending_and_single_submit(page: Page) -> None:
    print("verify_pending_and_single_submit: start", flush=True)
    await page.goto(VITE_URL)
    await page.wait_for_selector("[data-testid='prompt-input']")
    await page.evaluate(
        """
        () => {
          const submitButton = document.getElementById('chat-submit');
          submitButton?.click();
          submitButton?.click();
        }
        """
    )

    await page.wait_for_selector("#stop-button")
    await page.get_by_text("正在等待响应").wait_for()
    pending_state = await snapshot(page)
    pending_assistant = assistant_message(pending_state)

    assert pending_state["emitCounts"]["clientMessage"] == 1
    assert pending_state["generation"]["phase"] == "pending"
    assert pending_assistant.get("metadata", {}).get("generation_status") == "pending"

    await page.get_by_text(FULL_REPLY).wait_for(timeout=7000)
    finished_state = await snapshot(page)

    assert finished_state["emitCounts"]["clientMessage"] == 1
    assert finished_state["generation"]["phase"] == "idle"
    assert assistant_message(finished_state).get("output") == FULL_REPLY
    print("verify_pending_and_single_submit: ok", flush=True)


async def verify_stop_during_pending(page: Page) -> None:
    print("verify_stop_during_pending: start", flush=True)
    await page.goto(VITE_URL)
    await page.wait_for_selector("[data-testid='prompt-input']")
    await page.locator("#chat-submit").click()
    await page.wait_for_selector("#stop-button")
    await page.locator("#stop-button").click()

    await page.get_by_text("已停止").wait_for(timeout=3000)
    await page.wait_for_function(
        "() => window.__CHAINLIT_E2E__?.snapshot()?.generation.phase === 'idle'"
    )
    stopped_state = await snapshot(page)
    stopped_assistant = assistant_message(stopped_state)

    assert stopped_state["emitCounts"]["clientMessage"] == 1
    assert stopped_state["emitCounts"]["stop"] == 1
    assert stopped_assistant.get("metadata", {}).get("generation_status") == "stopped"
    assert (stopped_assistant.get("output") or "") == ""
    print("verify_stop_during_pending: ok", flush=True)


async def verify_stop_during_streaming(page: Page) -> None:
    print("verify_stop_during_streaming: start", flush=True)
    await page.goto(VITE_URL)
    await page.wait_for_selector("[data-testid='prompt-input']")
    await page.locator("#chat-submit").click()

    await page.wait_for_function(
        """
        () => {
          const data = window.__CHAINLIT_E2E__?.snapshot();
          if (!data) return false;
          const assistant = data.messages.find(
            (message) => message.type === 'assistant_message'
          );
          return Boolean(assistant?.output?.length);
        }
        """
    )

    streaming_state = await snapshot(page)
    partial_output = assistant_message(streaming_state).get("output") or ""
    if not partial_output:
        raise AssertionError("Expected partial assistant output before stopping")

    await page.locator("#stop-button").click()
    await page.get_by_text("已停止").wait_for(timeout=3000)
    await page.wait_for_timeout(1200)
    stopped_state = await snapshot(page)
    stopped_assistant = assistant_message(stopped_state)

    assert stopped_state["emitCounts"]["clientMessage"] == 1
    assert stopped_state["emitCounts"]["stop"] == 1
    assert stopped_state["generation"]["phase"] == "idle"
    assert stopped_assistant.get("metadata", {}).get("generation_status") == "stopped"
    assert (stopped_assistant.get("output") or "") == partial_output
    print("verify_stop_during_streaming: ok", flush=True)


async def run_browser_checks() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await verify_pending_and_single_submit(page)
            await verify_stop_during_pending(page)
            await verify_stop_during_streaming(page)
        finally:
            await browser.close()


def main() -> int:
    print("chat_lifecycle_browser_check: starting vite", flush=True)
    with VITE_LOG.open("w", encoding="utf-8") as log_file:
        server = subprocess.Popen(
            [
                "pnpm",
                "exec",
                "vite",
                "--host",
                "127.0.0.1",
                "--port",
                "4173",
                "--strictPort",
            ],
            cwd=FRONTEND_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

        try:
            wait_for_server(VITE_HEALTH_URL)
            print("chat_lifecycle_browser_check: vite ready", flush=True)
            asyncio.run(run_browser_checks())
            print("chat_lifecycle_browser_check: ok")
            return 0
        finally:
            if server.poll() is None:
                server.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    server.wait(timeout=10)
            if server.poll() is None:
                server.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI failure path
        print(f"chat_lifecycle_browser_check: failed: {exc}", file=sys.stderr)
        raise
