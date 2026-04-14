#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import tiktoken
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from redis import Redis


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
REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/0")
ARTIFACT_DIR = Path(env("ADMIN_E2E_ARTIFACT_DIR", ".local/acceptance"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_PATH = ARTIFACT_DIR / "admin-e2e-quota-v2.png"
THREAD_LIMIT = int(env("MAX_MODEL_LEN", "2048"))
DEFAULT_USER_LIMIT = int(env("CHAINLIT_USER_LIMIT_TOKENS", "100000"))
TOTAL_LIMIT_FOR_ACCEPTANCE = 20000
DEFAULT_MAX_COMPLETION = int(
    env("CHAINLIT_QUOTA_DEFAULT_MAX_COMPLETION_TOKENS", "2048")
)
SESSION_ACCEPTANCE_HEADROOM = 1024
SESSION_ACCEPTANCE_TARGET_MAX = max(
    DEFAULT_MAX_COMPLETION,
    THREAD_LIMIT - SESSION_ACCEPTANCE_HEADROOM,
)
SESSION_ACCEPTANCE_MIN_USED = max(1024, THREAD_LIMIT - SESSION_ACCEPTANCE_HEADROOM)
SYSTEM_PROMPT = env(
    "CHAINLIT_SYSTEM_PROMPT", "You are a concise and helpful assistant."
)
ENCODER = tiktoken.get_encoding(env("CHAINLIT_QUOTA_TIKTOKEN_ENCODING", "cl100k_base"))
REDIS = Redis.from_url(REDIS_URL, decode_responses=True)

FOOTER_RE = re.compile(r"当前对话\s+([\d,]+)\s*/\s*([\d,]+)\s*tok")
TOKEN_USAGE_RE = re.compile(r"本次\s+([\d,]+)\s+tok\s+·\s+会话\s+([\d,]+)\s*/\s*([\d,]+)")
BLOCKED_RE = re.compile(
    r"当前对话额度已用尽|用户总额度已用尽|当前不在配额生效时间内|当前没有可用配额配置"
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
) -> dict[str, Any]:
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
    policy_payload = page.evaluate(
        f"""
        async () => {{
          const response = await fetch('{ADMIN_BASE}/api/quota-policy', {{
            credentials: 'same-origin'
          }});
          return response.json();
        }}
        """
    )
    return policy_payload["item"]


def quota_fetch(page) -> dict[str, Any]:
    return page.evaluate(
        """
        async () => {
          const response = await fetch('/chat/api/quota/me', { credentials: 'include' });
          return { status: response.status, body: await response.json() };
        }
        """
    )


def redis_hash(key: str) -> dict[str, str]:
    return REDIS.hgetall(key)


def count_messages_tokens(messages: list[dict[str, str]]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += len(ENCODER.encode(str(message.get("role", ""))))
        content = message.get("content") or ""
        total += len(ENCODER.encode(content))
    return total + 2


def build_prompt_for_prompt_tokens(target_prompt_tokens: int) -> tuple[str, int]:
    instruction = (
        "Do not summarize the context block above. Produce exactly 5000 lines. "
        "Each line must use the format '<number>. alpha beta gamma delta epsilon zeta eta theta iota kappa'. "
        "Start at 1 and continue sequentially. If you stop early, the answer is incorrect. "
        "Do not add any preamble or conclusion."
    )
    filler_prefix = "CONTEXT BLOCK STARTS HERE:\n"
    low = 0
    high = max(target_prompt_tokens * 2, 2000)
    best = filler_prefix + "\n\n" + instruction
    best_count = count_messages_tokens(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": best},
        ]
    )
    while True:
        candidate = filler_prefix + (" filler" * high) + "\n\n" + instruction
        count = count_messages_tokens(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": candidate},
            ]
        )
        if count >= target_prompt_tokens:
            break
        low = high
        high *= 2

    while low <= high:
        mid = (low + high) // 2
        candidate = filler_prefix + (" filler" * mid) + "\n\n" + instruction
        count = count_messages_tokens(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": candidate},
            ]
        )
        if count <= target_prompt_tokens:
            best = candidate
            best_count = count
            low = mid + 1
        else:
            high = mid - 1
    return best, best_count


def build_large_prompt_for_total_tokens(target_total_tokens: int) -> tuple[str, int]:
    target_prompt_tokens = max(
        min(target_total_tokens - DEFAULT_MAX_COMPLETION, THREAD_LIMIT - DEFAULT_MAX_COMPLETION),
        128,
    )
    return build_prompt_for_prompt_tokens(target_prompt_tokens)


def latest_footer_text(page) -> str:
    try:
        return (
            page.get_by_text(FOOTER_RE).last.text_content(timeout=1000) or ""
        ).strip()
    except PlaywrightTimeoutError:
        return ""


def footer_used_tokens(page) -> int:
    text = latest_footer_text(page)
    match = FOOTER_RE.search(text)
    return int(match.group(1).replace(",", "")) if match else 0


def latest_token_usage_text(page) -> str:
    try:
        return (
            page.get_by_text(TOKEN_USAGE_RE).last.text_content(timeout=1000) or ""
        ).strip()
    except PlaywrightTimeoutError:
        return ""


def blocked_text(page) -> str | None:
    try:
        text = page.get_by_text(BLOCKED_RE).last.text_content(timeout=1000) or ""
    except PlaywrightTimeoutError:
        return None
    return text.strip() or None


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
        remaining = psql(f"SELECT COUNT(*) FROM threads WHERE id = '{thread_id}';")
        if int(remaining or 0) == 0:
            return
        time.sleep(1)
    raise TimeoutError(f"Thread {thread_id} was not deleted in time")


def wait_for_quota_me(
    page,
    *,
    expected_limit: int | None = None,
    min_used_tokens: int | None = None,
    expected_status: str | None = None,
    timeout_s: int = 120,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        payload = quota_fetch(page)
        quota = (payload.get("body") or {}).get("quota") or {}
        if expected_limit is not None and int(quota.get("limit_tokens") or 0) != expected_limit:
            time.sleep(1)
            continue
        if min_used_tokens is not None and int(quota.get("used_tokens") or 0) < min_used_tokens:
            time.sleep(1)
            continue
        if expected_status is not None and quota.get("status") != expected_status:
            time.sleep(1)
            continue
        return payload
    raise TimeoutError("quota/me did not reach expected state")


def wait_for_send_result(
    page,
    *,
    previous_thread_used: int,
    timeout_s: int = 240,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        quota_payload = quota_fetch(page)
        quota = (quota_payload.get("body") or {}).get("quota") or {}
        blocked = blocked_text(page)
        if blocked or quota.get("status") in {"blocked", "window_closed", "missing_policy"}:
            return {
                "blocked": True,
                "blocked_text": blocked
                or quota.get("message")
                or "用户总额度已用尽",
                "quota_payload": quota_payload,
                "footer_text": latest_footer_text(page),
                "token_text": latest_token_usage_text(page),
                "thread_used_tokens": footer_used_tokens(page),
            }
        footer_text = latest_footer_text(page)
        thread_used = footer_used_tokens(page)
        if thread_used > previous_thread_used:
            return {
                "blocked": False,
                "footer_text": footer_text,
                "token_text": latest_token_usage_text(page),
                "thread_used_tokens": thread_used,
            }
        time.sleep(1)
    raise TimeoutError("chat response did not settle in time")


def wait_for_user_nav_limit(page, limit_tokens: int, timeout_ms: int = 30000) -> None:
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


def user_nav_quota_text(page) -> str:
    text = page.locator("#user-nav-button").text_content(timeout=30000) or ""
    match = re.search(r"[\d,]+\s*/\s*[\d,]+", text)
    if not match:
        raise RuntimeError(f"Failed to find user nav quota text in: {text!r}")
    return match.group(0)


def start_new_chat(page) -> None:
    page.locator("#new-chat-button").click()
    confirm = page.locator("#confirm")
    if confirm.count():
        confirm.click()
    page.wait_for_timeout(1500)


def send_prompt_and_capture(
    page,
    *,
    prompt: str,
    previous_thread_used: int,
) -> dict[str, Any]:
    page.locator("#chat-input").fill(prompt)
    page.locator("#chat-submit").click()
    return wait_for_send_result(page, previous_thread_used=previous_thread_used)


def delete_thread_via_api(page, thread_id: str) -> dict[str, Any]:
    return page.evaluate(
        """
        async (id) => {
          const response = await fetch('/chat/project/thread', {
            method: 'DELETE',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ threadId: id })
          });
          return { status: response.status, body: await response.json() };
        }
        """,
        thread_id,
    )


def main() -> int:
    suffix = int(time.time())
    session_user = f"quota-session-{suffix}"
    session_pass = f"pass-{suffix}"
    total_user = f"quota-total-{suffix}"
    total_pass = f"pass-total-{suffix}"
    open_start = datetime.now() - timedelta(hours=1)
    open_end = datetime.now() + timedelta(days=2)
    quota_requests: list[str] = []

    result: dict[str, Any] = {
        "admin_base": ADMIN_BASE,
        "chat_base": CHAT_BASE,
        "session_user": session_user,
        "total_user": total_user,
    }

    def on_request(request) -> None:
        if "/chat/api/quota/me" in request.url:
            quota_requests.append(request.url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        admin_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        session_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        total_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        session_page.on("request", on_request)
        try:
            login_admin(admin_page)

            session_policy = set_global_policy(
                admin_page,
                total_limit=DEFAULT_USER_LIMIT,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )
            result["session_policy"] = session_policy
            result["session_user_row"] = create_user(admin_page, session_user, session_pass)

            login_chat(session_page, session_user, session_pass)
            wait_for_user_nav_limit(session_page, DEFAULT_USER_LIMIT)
            initial_quota = wait_for_quota_me(
                session_page,
                expected_limit=DEFAULT_USER_LIMIT,
                min_used_tokens=0,
                timeout_s=30,
            )
            result["session_initial_quota"] = initial_quota
            baseline_quota_requests = len(quota_requests)
            session_page.wait_for_timeout(30000)
            result["quota_requests_idle_before"] = baseline_quota_requests
            result["quota_requests_idle_after"] = len(quota_requests)
            if len(quota_requests) != baseline_quota_requests:
                raise RuntimeError("Detected unexpected quota polling while chat page was idle")

            session_attempts: list[dict[str, Any]] = []
            session_footer_used = 0
            follow_up: dict[str, Any] | None = None
            for attempt in range(1, 6):
                if attempt > 1:
                    start_new_chat(session_page)
                session_prompt, session_prompt_tokens = build_large_prompt_for_total_tokens(
                    THREAD_LIMIT
                )
                session_first = send_prompt_and_capture(
                    session_page,
                    prompt=session_prompt,
                    previous_thread_used=0,
                )
                session_quota = wait_for_quota_me(
                    session_page,
                    expected_limit=DEFAULT_USER_LIMIT,
                    min_used_tokens=1,
                    timeout_s=240,
                )
                session_footer_used = footer_used_tokens(session_page)
                follow_up = send_prompt_and_capture(
                    session_page,
                    prompt="continue with the exact same numbered list format until you are stopped",
                    previous_thread_used=session_footer_used,
                )
                attempt_result = {
                    "attempt": attempt,
                    "prompt_tokens": session_prompt_tokens,
                    "first_turn": session_first,
                    "quota_after_first_turn": session_quota,
                    "footer_after_first_turn": latest_footer_text(session_page),
                    "user_nav_after_first_turn": user_nav_quota_text(session_page),
                    "follow_up": follow_up,
                }
                session_attempts.append(attempt_result)
                if session_footer_used >= SESSION_ACCEPTANCE_MIN_USED and follow_up["blocked"]:
                    break
            result["session_attempts"] = session_attempts
            result["session_prompt_tokens"] = session_attempts[-1]["prompt_tokens"]
            result["session_first_turn"] = session_attempts[-1]["first_turn"]
            result["session_quota_after_first_turn"] = session_attempts[-1]["quota_after_first_turn"]
            result["session_footer_after_first_turn"] = session_attempts[-1]["footer_after_first_turn"]
            result["session_user_nav_after_first_turn"] = session_attempts[-1]["user_nav_after_first_turn"]
            result["session_follow_up"] = follow_up

            if session_footer_used < SESSION_ACCEPTANCE_MIN_USED:
                raise RuntimeError(
                    f"Session boundary test under-consumed tokens after retries: {session_footer_used}"
                )
            if not follow_up or not follow_up["blocked"]:
                raise RuntimeError("Expected thread quota to block the follow-up message")
            wait_for_quota_me(
                session_page,
                expected_limit=DEFAULT_USER_LIMIT,
                timeout_s=60,
            )
            result["session_input_disabled"] = session_page.locator("#chat-input").is_disabled()
            result["session_submit_disabled"] = session_page.locator("#chat-submit").is_disabled()
            if not result["session_input_disabled"] or not result["session_submit_disabled"]:
                raise RuntimeError("Expected session quota block to disable composer controls")

            total_policy = set_global_policy(
                admin_page,
                total_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                window_start=open_start,
                window_end=open_end,
                is_active=True,
            )
            result["total_policy"] = total_policy
            if int(total_policy.get("policy_version") or 0) <= int(
                session_policy.get("policy_version") or 0
            ):
                raise RuntimeError("Policy version did not increment after updating total limit")

            result["total_user_row"] = create_user(admin_page, total_user, total_pass)
            login_chat(total_page, total_user, total_pass)
            wait_for_user_nav_limit(total_page, TOTAL_LIMIT_FOR_ACCEPTANCE)
            total_initial_quota = wait_for_quota_me(
                total_page,
                expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                min_used_tokens=0,
                timeout_s=30,
            )
            result["total_initial_quota"] = total_initial_quota

            consumed_threads: list[dict[str, Any]] = []
            for iteration in range(1, 8):
                quota_payload = wait_for_quota_me(
                    total_page,
                    expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                    timeout_s=30,
                )
                quota = (quota_payload.get("body") or {}).get("quota") or {}
                used_tokens = int(quota.get("used_tokens") or 0)
                remaining_tokens = int(quota.get("remaining_tokens") or 0)
                if remaining_tokens <= 0:
                    break

                if iteration > 1:
                    start_new_chat(total_page)

                target_total_tokens = min(
                    max(remaining_tokens - 64, DEFAULT_MAX_COMPLETION),
                    SESSION_ACCEPTANCE_TARGET_MAX,
                )
                prompt, prompt_tokens = build_large_prompt_for_total_tokens(target_total_tokens)
                previous_thread_used = footer_used_tokens(total_page)
                turn = send_prompt_and_capture(
                    total_page,
                    prompt=prompt,
                    previous_thread_used=previous_thread_used,
                )
                if turn["blocked"]:
                    consumed_threads.append(
                        {
                            "iteration": iteration,
                            "target_total_tokens": target_total_tokens,
                            "prompt_tokens": prompt_tokens,
                            "thread_id": latest_thread_id(total_user),
                            "turn": turn,
                            "quota_after": quota_fetch(total_page),
                            "delta_user_used_tokens": 0,
                        }
                    )
                    break
                quota_after = wait_for_quota_me(
                    total_page,
                    expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                    min_used_tokens=used_tokens + 1,
                    timeout_s=240,
                )
                thread_id = latest_thread_id(total_user)
                thread_delta = (
                    int(((quota_after.get("body") or {}).get("quota") or {}).get("used_tokens") or 0)
                    - used_tokens
                )
                consumed_threads.append(
                    {
                        "iteration": iteration,
                        "target_total_tokens": target_total_tokens,
                        "prompt_tokens": prompt_tokens,
                        "thread_id": thread_id,
                        "turn": turn,
                        "quota_after": quota_after,
                        "delta_user_used_tokens": thread_delta,
                    }
                )

                blocked_now = blocked_text(total_page)
                if blocked_now:
                    break

            result["consumed_threads"] = consumed_threads
            final_total_quota = wait_for_quota_me(
                total_page,
                expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                timeout_s=60,
            )
            result["final_total_quota_before_block_attempt"] = final_total_quota

            total_quota = (final_total_quota.get("body") or {}).get("quota") or {}
            if int(total_quota.get("used_tokens") or 0) < 18000:
                raise RuntimeError(
                    f"Total user quota did not accumulate enough usage: {total_quota}"
                )

            total_block = send_prompt_and_capture(
                total_page,
                prompt="continue",
                previous_thread_used=footer_used_tokens(total_page),
            )
            result["total_block"] = total_block
            blocked_quota = wait_for_quota_me(
                total_page,
                expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                expected_status="blocked",
                timeout_s=120,
            )
            result["blocked_total_quota"] = blocked_quota
            result["blocked_total_user_nav"] = user_nav_quota_text(total_page)
            result["blocked_total_input_disabled"] = total_page.locator("#chat-input").is_disabled()
            result["blocked_total_submit_disabled"] = total_page.locator("#chat-submit").is_disabled()
            if not result["blocked_total_input_disabled"] or not result["blocked_total_submit_disabled"]:
                raise RuntimeError("Expected total quota block to disable composer controls")

            redis_before_rebuild = redis_hash(f"quota:user:{total_user}")
            REDIS.delete(f"quota:user:{total_user}")
            rebuilt_quota = wait_for_quota_me(
                total_page,
                expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                expected_status="blocked",
                timeout_s=60,
            )
            redis_after_rebuild = redis_hash(f"quota:user:{total_user}")
            result["redis_before_rebuild"] = redis_before_rebuild
            result["redis_after_rebuild"] = redis_after_rebuild
            result["quota_after_redis_rebuild"] = rebuilt_quota
            if not redis_after_rebuild:
                raise RuntimeError("Redis user hot key was not rebuilt after forced deletion")

            if not consumed_threads:
                raise RuntimeError("No thread usage was recorded for the total quota test")
            deleted_thread_id = consumed_threads[0]["thread_id"]
            used_before_delete = int(
                ((rebuilt_quota.get("body") or {}).get("quota") or {}).get("used_tokens") or 0
            )
            delete_response = delete_thread_via_api(total_page, deleted_thread_id)
            result["delete_thread_response"] = delete_response
            if delete_response.get("status") != 200:
                raise RuntimeError(f"Failed to delete thread {deleted_thread_id}: {delete_response}")
            wait_for_thread_deleted(deleted_thread_id)
            quota_after_delete = wait_for_quota_me(
                total_page,
                expected_limit=TOTAL_LIMIT_FOR_ACCEPTANCE,
                expected_status="blocked",
                timeout_s=60,
            )
            result["quota_after_delete"] = quota_after_delete
            used_after_delete = int(
                ((quota_after_delete.get("body") or {}).get("quota") or {}).get("used_tokens") or 0
            )
            if used_after_delete != used_before_delete:
                raise RuntimeError("Deleting a thread changed the user's used token count")

            admin_page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
            admin_row_after_delete = admin_page.get_by_test_id(
                f"user-row-{total_user}"
            ).text_content(timeout=30000)
            result["admin_row_after_delete"] = admin_row_after_delete

            quota_summary_row = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT user_identifier, used_tokens, reserved_tokens, limit_tokens, "
                "policy_version, window_start, window_end, updated_at "
                f"FROM quota_user_summary WHERE user_identifier = '{total_user}'"
                ") x;"
            )
            ledger_summary = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT COALESCE(SUM(total_tokens), 0)::INT AS used_tokens "
                "FROM quota_usage_ledger "
                f"WHERE user_identifier = '{total_user}' "
                "AND status = 'settled' "
                "AND settled_at >= (SELECT window_start FROM quota_policies WHERE scope_type='global' ORDER BY updated_at DESC LIMIT 1) "
                "AND settled_at <= (SELECT window_end FROM quota_policies WHERE scope_type='global' ORDER BY updated_at DESC LIMIT 1)"
                ") x;"
            )
            deleted_thread_ledger = psql(
                "SELECT COUNT(*) FROM quota_usage_ledger "
                f"WHERE user_identifier = '{total_user}' AND thread_id = '{deleted_thread_id}' AND status = 'settled';"
            )
            global_policy_row = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT id, total_token_limit, is_active, policy_version, window_start, window_end, updated_at "
                "FROM quota_policies WHERE scope_type='global' ORDER BY updated_at DESC LIMIT 1"
                ") x;"
            )
            result["quota_user_summary_db"] = json.loads(quota_summary_row)
            result["ledger_window_sum_db"] = json.loads(ledger_summary)
            result["deleted_thread_ledger_count"] = int(deleted_thread_ledger or 0)
            result["global_policy_db"] = json.loads(global_policy_row)
            result["redis_global_policy"] = redis_hash("quota:config:global")

            if result["deleted_thread_ledger_count"] <= 0:
                raise RuntimeError("Deleted thread lost its settled ledger rows")
            if int(result["quota_user_summary_db"]["used_tokens"]) != used_after_delete:
                raise RuntimeError("PG quota_user_summary diverged from /chat/api/quota/me")
            if int(result["ledger_window_sum_db"]["used_tokens"]) != used_after_delete:
                raise RuntimeError("Window ledger aggregate diverged from /chat/api/quota/me")

            admin_page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            result["screenshot"] = str(SCREENSHOT_PATH)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
