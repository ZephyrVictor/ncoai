#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - operational script
    raise SystemExit(
        "playwright is not installed. Activate .venv and install runtime deps first."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / ".local" / "acceptance"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
HTTP = requests.Session()
HTTP.trust_env = False


@dataclass
class ThreadRecord:
    thread_id: str
    metadata: dict[str, Any]


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


VERBOSE = env("ACCEPTANCE_VERBOSE", "0").lower() in {"1", "true", "yes", "on"}


def progress(message: str) -> None:
    if VERBOSE:
        print(f"[acceptance] {message}", file=sys.stderr, flush=True)


def wait_for_message_article(page: Any, text: str, timeout: int = 30000) -> None:
    page.get_by_role("article").filter(has_text=text).first.wait_for(timeout=timeout)


def safe_screenshot(page: Any, path: Path) -> None:
    try:
        page.screenshot(path=str(path), full_page=True, timeout=5000)
    except Exception:
        pass


def service_origin(default: str) -> str:
    return default.rstrip("/")


def service_base_candidates() -> list[str]:
    primary = env("ACCEPTANCE_BASE_URL", "http://127.0.0.1:8088/chat").rstrip("/")
    fallback = env("CHAINLIT_DIRECT_URL", "http://127.0.0.1:3100/chat").rstrip("/")
    candidates: list[str] = []
    for candidate in (primary, fallback):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def capture_http(
    report: dict[str, Any],
    name: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> requests.Response | None:
    try:
        response = HTTP.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
        report[name] = {
            "ok": response.ok,
            "status": response.status_code,
            "body": response.text[:500],
        }
        return response
    except Exception as exc:  # pragma: no cover - operational script
        report[name] = {"ok": False, "error": str(exc)}
        return None


def resolve_chat_base_url(report: dict[str, Any]) -> str:
    for candidate in service_base_candidates():
        try:
            response = HTTP.get(candidate, timeout=10, allow_redirects=True)
            report.setdefault("chat_entry_checks", []).append(
                {
                    "candidate": candidate,
                    "status": response.status_code,
                    "final_url": response.url,
                    "ok": response.ok,
                }
            )
            if response.ok:
                return candidate
        except Exception as exc:  # pragma: no cover - operational script
            report.setdefault("chat_entry_checks", []).append(
                {"candidate": candidate, "ok": False, "error": str(exc)}
            )

    raise RuntimeError("No reachable chat entrypoint found.")


def wait_for_thread_record(
    database_url: str, user_identifier: str, timeout_s: int = 30
) -> ThreadRecord:
    deadline = time.time() + timeout_s
    escaped_user = user_identifier.replace("'", "''")
    query = (
        'SELECT id, COALESCE(metadata::text, \'{}\') '
        'FROM threads '
        f'WHERE "userIdentifier" = \'{escaped_user}\' '
        'ORDER BY "createdAt" DESC '
        'LIMIT 1;'
    )

    while time.time() < deadline:
        progress("querying latest thread metadata for first request")
        proc = subprocess.run(
            ["psql", database_url, "-Atq", "-F", "\t", "-c", query],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "psql query failed")

        line = proc.stdout.strip()
        if line:
            thread_id, metadata_raw = line.split("\t", 1)
            metadata = json.loads(metadata_raw or "{}")
            return ThreadRecord(thread_id=thread_id, metadata=metadata)

        time.sleep(1)

    raise TimeoutError("No persisted thread record found for acceptance user.")


def wait_for_request_id(
    database_url: str, user_identifier: str, expected_message: str, timeout_s: int = 45
) -> ThreadRecord:
    deadline = time.time() + timeout_s
    escaped_user = user_identifier.replace("'", "''")
    escaped_message = expected_message.replace("'", "''")
    query = (
        'SELECT t.id, COALESCE(t.metadata::text, \'{}\') '
        "FROM threads t "
        'JOIN steps s ON s."threadId" = t.id '
        f'WHERE t."userIdentifier" = \'{escaped_user}\' '
        f"AND s.type = 'user_message' AND s.output = '{escaped_message}' "
        'ORDER BY t."createdAt" DESC '
        'LIMIT 1;'
    )

    while time.time() < deadline:
        progress("waiting for first request_id to appear in thread metadata")
        proc = subprocess.run(
            ["psql", database_url, "-Atq", "-F", "\t", "-c", query],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "psql query failed")

        line = proc.stdout.strip()
        if line:
            thread_id, metadata_raw = line.split("\t", 1)
            metadata = json.loads(metadata_raw or "{}")
            if metadata.get("last_request_id"):
                return ThreadRecord(thread_id=thread_id, metadata=metadata)

        time.sleep(1)

    raise TimeoutError("Persisted thread metadata never received a last_request_id.")


def wait_for_thread_request_change(
    database_url: str,
    thread_id: str,
    previous_request_id: str,
    timeout_s: int = 60,
) -> ThreadRecord:
    deadline = time.time() + timeout_s
    escaped_thread_id = thread_id.replace("'", "''")
    query = (
        'SELECT id, COALESCE(metadata::text, \'{}\') '
        'FROM threads '
        f"WHERE id = '{escaped_thread_id}' "
        'LIMIT 1;'
    )

    while time.time() < deadline:
        progress(f"waiting for request id change on thread {thread_id}")
        proc = subprocess.run(
            ["psql", database_url, "-Atq", "-F", "\t", "-c", query],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "psql query failed")

        line = proc.stdout.strip()
        if line:
            current_thread_id, metadata_raw = line.split("\t", 1)
            metadata = json.loads(metadata_raw or "{}")
            request_id = metadata.get("last_request_id")
            if request_id and request_id != previous_request_id:
                return ThreadRecord(thread_id=current_thread_id, metadata=metadata)

        time.sleep(1)

    raise TimeoutError("Thread metadata did not receive a new last_request_id.")


def find_spend_log(
    litellm_origin: str,
    api_key: str,
    *,
    request_id: str,
    end_user: str,
) -> dict[str, Any]:
    start_date = date.today().isoformat()
    end_date = (date.today() + timedelta(days=1)).isoformat()
    response = HTTP.get(
        f"{litellm_origin}/spend/logs",
        params={
            "start_date": start_date,
            "end_date": end_date,
            "summarize": "false",
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected /spend/logs payload shape.")

    for item in payload:
        if item.get("request_id") == request_id and item.get("end_user") == end_user:
            return item

    raise LookupError(f"No LiteLLM spend log found for request_id={request_id}.")


def browser_acceptance(
    report: dict[str, Any],
    *,
    base_url: str,
    database_url: str,
    username: str,
    password: str,
    enable_thinking: bool,
) -> dict[str, Any]:
    unique = f"acceptance ping {int(time.time())}"
    follow_up = f"follow up {int(time.time())}"
    screenshot_path = ARTIFACT_DIR / "acceptance-last.png"

    outcome: dict[str, Any] = {
        "base_url": base_url,
        "login_required": False,
        "message_sent": unique,
        "follow_up_sent": follow_up,
    }

    with sync_playwright() as playwright:
        progress("launching browser for first-round acceptance")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            progress(f"opening {base_url}")
            page.goto(base_url, wait_until="commit", timeout=30000)
            page.wait_for_url("**/login", timeout=30000)
            outcome["login_required"] = True

            progress("submitting login form")
            page.fill("input#email", username)
            page.fill("input#password", password)
            page.get_by_role("button", name="Sign In").click()

            page.wait_for_url("**/chat*", timeout=30000)
            chat_input = page.locator("#chat-input")
            chat_input.wait_for(state="visible", timeout=30000)
            progress("sending first chat message")
            chat_input.fill(unique)
            page.locator("#chat-submit").click()

            wait_for_message_article(page, unique, timeout=30000)
            progress("first user message visible, waiting for persisted request id")
            persisted_thread = wait_for_request_id(
                database_url=database_url,
                user_identifier=username,
                expected_message=unique,
                timeout_s=120,
            )

            outcome["chat_url_after_first_reply"] = page.url
            outcome["thread_id"] = persisted_thread.thread_id
            outcome["last_request_id"] = persisted_thread.metadata.get("last_request_id")
            outcome["thinking_profile_visible"] = bool(
                enable_thinking and page.locator("#chat-profiles").count()
            )
            page.context.storage_state(path=str(ARTIFACT_DIR / "storage-state.json"))
        except PlaywrightTimeoutError as exc:
            safe_screenshot(page, screenshot_path)
            raise RuntimeError(
                f"Playwright timeout: {exc}. Screenshot: {screenshot_path}"
            ) from exc
        except Exception:
            safe_screenshot(page, screenshot_path)
            raise
        finally:
            browser.close()

    report["browser_acceptance"] = outcome
    return outcome


def browser_resume_validation(
    *,
    base_url: str,
    username: str,
    password: str,
    database_url: str,
    thread_id: str,
    previous_request_id: str,
    original_message: str,
    follow_up_message: str,
) -> dict[str, Any]:
    screenshot_path = ARTIFACT_DIR / "acceptance-resume.png"
    storage_state_path = ARTIFACT_DIR / "storage-state.json"

    with sync_playwright() as playwright:
        progress("launching browser for resume validation")
        browser = playwright.chromium.launch(headless=True)
        context = (
            browser.new_context(
                viewport={"width": 1440, "height": 1100},
                storage_state=str(storage_state_path),
            )
            if storage_state_path.exists()
            else browser.new_context(viewport={"width": 1440, "height": 1100})
        )
        page = context.new_page()
        try:
            progress(f"opening {base_url} for resume validation")
            page.goto(base_url, wait_until="commit", timeout=30000)
            try:
                page.wait_for_url("**/login", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            if "/login" in page.url or page.locator("input#email").count():
                progress("login required during resume validation")
                page.fill("input#email", username)
                page.fill("input#password", password)
                page.get_by_role("button", name="Sign In").click()
                page.wait_for_url("**/chat*", timeout=30000)

            progress(f"opening thread {thread_id}")
            page.goto(urljoin(f"{base_url}/", f"thread/{thread_id}"), timeout=30000)
            try:
                page.wait_for_url("**/login", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            if "/login" in page.url or page.locator("input#email").count():
                progress("thread route redirected to login, authenticating again")
                page.fill("input#email", username)
                page.fill("input#password", password)
                page.get_by_role("button", name="Sign In").click()
                page.wait_for_url("**/chat*", timeout=30000)
                page.goto(urljoin(f"{base_url}/", f"thread/{thread_id}"), timeout=30000)

            wait_for_message_article(page, original_message, timeout=30000)

            chat_input = page.locator("#chat-input")
            chat_input.wait_for(state="visible", timeout=30000)
            progress("sending follow-up message on resumed thread")
            chat_input.fill(follow_up_message)
            page.locator("#chat-submit").click()
            wait_for_message_article(page, follow_up_message, timeout=30000)

            progress("waiting for resumed thread request id update")
            updated_thread = wait_for_thread_request_change(
                database_url=database_url,
                thread_id=thread_id,
                previous_request_id=previous_request_id,
                timeout_s=120,
            )

            return {
                "thread_id": thread_id,
                "resume_url": page.url,
                "ok": True,
                "last_request_id": updated_thread.metadata.get("last_request_id"),
            }
        except PlaywrightTimeoutError as exc:
            safe_screenshot(page, screenshot_path)
            raise RuntimeError(
                f"Resume validation timeout: {exc}. Screenshot: {screenshot_path}"
            ) from exc
        except Exception:
            safe_screenshot(page, screenshot_path)
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    report: dict[str, Any] = {"ok": False}

    vllm_origin = service_origin(env("VLLM_ORIGIN", "http://127.0.0.1:12233"))
    litellm_origin = service_origin(env("LITELLM_ORIGIN", "http://127.0.0.1:33455"))
    litellm_api_key = env("LITELLM_MASTER_KEY", env("CHAINLIT_LITELLM_API_KEY", "sk-local-dev"))
    database_url = env(
        "DATABASE_URL",
        "postgresql://ncoai:ncoai_dev@127.0.0.1:55432/ncoai_chat",
    )
    username = env("CHAINLIT_APP_USERNAME", "acceptance")
    password = env("CHAINLIT_APP_PASSWORD", "acceptance123")
    enable_thinking = env("CHAINLIT_ENABLE_THINKING", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    try:
        progress("resolving reachable chat entrypoint")
        chat_base_url = resolve_chat_base_url(report)
        report["selected_chat_base_url"] = chat_base_url

        progress("capturing service health")
        capture_http(report, "vllm_health", "GET", f"{vllm_origin}/health", timeout=15)
        capture_http(report, "vllm_models", "GET", f"{vllm_origin}/v1/models", timeout=15)
        capture_http(
            report,
            "litellm_models",
            "GET",
            f"{litellm_origin}/v1/models",
            headers={"Authorization": f"Bearer {litellm_api_key}"},
            timeout=15,
        )

        progress("running browser acceptance")
        browser_outcome = browser_acceptance(
            report,
            base_url=chat_base_url,
            database_url=database_url,
            username=username,
            password=password,
            enable_thinking=enable_thinking,
        )

        thread_record = ThreadRecord(
            thread_id=browser_outcome["thread_id"],
            metadata={"last_request_id": browser_outcome["last_request_id"]},
        )
        report["thread_persistence"] = {
            "thread_id": thread_record.thread_id,
            "metadata": thread_record.metadata,
        }

        progress("running browser resume validation")
        report["resume_validation"] = browser_resume_validation(
            base_url=chat_base_url,
            username=username,
            password=password,
            database_url=database_url,
            thread_id=thread_record.thread_id,
            previous_request_id=thread_record.metadata["last_request_id"],
            original_message=browser_outcome["message_sent"],
            follow_up_message=browser_outcome["follow_up_sent"],
        )

        progress("querying LiteLLM spend logs")
        spend_log = find_spend_log(
            litellm_origin,
            litellm_api_key,
            request_id=thread_record.metadata["last_request_id"],
            end_user=username,
        )
        report["litellm_usage"] = {
            "request_id": spend_log.get("request_id"),
            "model": spend_log.get("model"),
            "total_tokens": spend_log.get("total_tokens"),
            "prompt_tokens": spend_log.get("prompt_tokens"),
            "completion_tokens": spend_log.get("completion_tokens"),
            "end_user": spend_log.get("end_user"),
        }

        report["ok"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # pragma: no cover - operational script
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
