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
SCREENSHOT_PATH = ARTIFACT_DIR / "admin-e2e-import.png"


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


def create_user(page, identifier: str, password: str, display_name: str) -> None:
    page.goto(f"{ADMIN_BASE}/users", wait_until="domcontentloaded", timeout=30000)
    page.locator("#user-identifier").fill(identifier)
    page.locator("#user-display-name").fill(display_name)
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


def main() -> int:
    suffix = int(time.time())
    existing_user = f"csv-update-{suffix}"
    new_user = f"csv-new-{suffix}"
    bad_user = f"csv-bad-{suffix}"
    existing_old_password = f"old-pass-{suffix}"
    existing_new_password = f"new-pass-{suffix}"
    new_password = f"import-pass-{suffix}"
    csv_path = ARTIFACT_DIR / f"admin-import-{suffix}.csv"
    open_start = datetime.now() - timedelta(hours=1)
    open_end = datetime.now() + timedelta(days=2)

    csv_path.write_text(
        "\n".join(
            [
                "identifier,password,display_name,is_active",
                f"{existing_user},{existing_new_password},Updated {suffix},true",
                f"{new_user},{new_password},Imported {suffix},true",
                f"{bad_user},,Broken {suffix},true",
            ]
        ),
        encoding="utf-8",
    )

    result: dict[str, object] = {
        "existing_user": existing_user,
        "new_user": new_user,
        "csv_path": str(csv_path),
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        admin_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        imported_user_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        updated_user_page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login_admin(admin_page)
            set_global_policy(
                admin_page,
                total_limit=100000,
                window_start=open_start,
                window_end=open_end,
            )
            create_user(
                admin_page,
                existing_user,
                existing_old_password,
                f"Original {suffix}",
            )
            result["existing_row_before_import"] = admin_page.get_by_test_id(
                f"user-row-{existing_user}"
            ).text_content()
            admin_page.evaluate(
                """
                () => {
                  window.sessionStorage.removeItem('adminFlash');
                  const flash = document.getElementById('flash-banner');
                  if (flash) {
                    flash.textContent = '';
                    flash.className = 'flash hidden';
                  }
                }
                """
            )

            admin_page.locator("#import-file").set_input_files(str(csv_path))
            result["confirm_disabled_before_preview"] = admin_page.locator(
                "#import-confirm-button"
            ).is_disabled()

            admin_page.locator("#import-preview-button").click()
            admin_page.wait_for_function(
                """
                () => {
                  const summary = document.querySelector('[data-testid="import-summary"]');
                  const confirm = document.querySelector('#import-confirm-button');
                  return !!summary?.textContent?.includes('总行数 3') && confirm && !confirm.disabled;
                }
                """,
                timeout=30000,
            )
            preview_summary = admin_page.get_by_test_id("import-summary").text_content()
            preview_table = admin_page.get_by_test_id("import-preview-table").text_content()
            result["preview_summary"] = preview_summary
            result["preview_table"] = preview_table
            result["confirm_enabled_after_preview"] = not admin_page.locator(
                "#import-confirm-button"
            ).is_disabled()

            admin_page.locator("#import-confirm-button").click()
            admin_page.wait_for_function(
                """
                () => {
                  const flash = document.querySelector('[data-testid="flash-banner"]');
                  return !!flash?.textContent?.includes('CSV 导入完成');
                }
                """,
                timeout=30000,
            )
            flash_text = admin_page.get_by_test_id("flash-banner").text_content()
            result["flash_after_import"] = flash_text
            admin_page.get_by_test_id(f"user-row-{new_user}").wait_for(timeout=30000)
            admin_page.get_by_test_id(f"user-row-{existing_user}").wait_for(timeout=30000)
            result["new_user_row"] = admin_page.get_by_test_id(
                f"user-row-{new_user}"
            ).text_content()
            result["updated_user_row"] = admin_page.get_by_test_id(
                f"user-row-{existing_user}"
            ).text_content()

            login_chat(imported_user_page, new_user, new_password)
            login_chat(updated_user_page, existing_user, existing_new_password)
            result["imported_user_login_url"] = imported_user_page.url
            result["updated_user_login_url"] = updated_user_page.url

            import_job = psql(
                "SELECT row_to_json(x)::text FROM ("
                "SELECT id, preview_only, status, total_rows, success_rows, failure_rows "
                "FROM user_import_jobs ORDER BY created_at DESC LIMIT 1"
                ") x;"
            )
            import_rows = psql(
                "SELECT COALESCE(json_agg(x ORDER BY x.row_number)::text, '[]') "
                "FROM ("
                "SELECT row_number, identifier, action, status, error_message "
                "FROM user_import_job_rows "
                "WHERE job_id = (SELECT id FROM user_import_jobs ORDER BY created_at DESC LIMIT 1)"
                ") x;"
            )
            managed_users = psql(
                "SELECT COALESCE(json_agg(x ORDER BY x.identifier)::text, '[]') "
                "FROM ("
                "SELECT identifier, display_name, is_active, is_deleted "
                "FROM managed_users "
                f"WHERE identifier IN ('{existing_user}', '{new_user}')"
                ") x;"
            )
            result["latest_import_job"] = json.loads(import_job)
            result["latest_import_rows"] = json.loads(import_rows)
            result["managed_users_db"] = json.loads(managed_users)

            admin_page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            result["screenshot"] = str(SCREENSHOT_PATH)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
