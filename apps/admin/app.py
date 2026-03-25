from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tiktoken
from redis.asyncio import Redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from database import create_app_engine
from ncoai_shared.bootstrap import sync_global_quota_policy_cache as sync_global_cache
from ncoai_shared.controlplane import ControlPlaneRepository
from ncoai_shared.security import hash_password, verify_password
from settings import SETTINGS

APP_DIR = Path(__file__).resolve().parent
DISPLAY_TZ = ZoneInfo(SETTINGS.display_timezone)
ENGINE = create_app_engine(SETTINGS.database_async_url)
REDIS = Redis.from_url(SETTINGS.redis_url, decode_responses=True)
ENCODER = tiktoken.get_encoding(SETTINGS.tiktoken_encoding)


def count_messages_tokens(messages: list[dict[str, str]]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += len(ENCODER.encode(str(message.get("role", ""))))
        total += len(ENCODER.encode(message.get("content") or ""))
    return total + 2


MINIMUM_USER_REQUEST_TOKENS = (
    count_messages_tokens(
        [
            {"role": "system", "content": SETTINGS.system_prompt},
            {"role": "user", "content": ""},
        ]
    )
    + SETTINGS.prompt_safety_tokens
)
REPOSITORY = ControlPlaneRepository(
    ENGINE,
    minimum_available_tokens_for_request=MINIMUM_USER_REQUEST_TOKENS,
)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def format_datetime_input(value: str | None) -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%dT%H:%M")


def format_number(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


templates.env.filters["format_datetime"] = format_datetime
templates.env.filters["format_datetime_input"] = format_datetime_input
templates.env.filters["format_number"] = format_number


class LoginPayload(BaseModel):
    username: str
    password: str


class UserPayload(BaseModel):
    identifier: str
    display_name: str
    password: str | None = None
    is_active: bool = True


class PolicyPayload(BaseModel):
    total_token_limit: int = Field(..., gt=0)
    is_active: bool = True
    window_start_local: str
    window_end_local: str


class ImportPayload(BaseModel):
    csv_text: str
    confirm: bool = False


def parse_local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TZ)
    return parsed.astimezone(timezone.utc)


def parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def sync_global_quota_policy_cache(
    policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return await sync_global_cache(ENGINE, REDIS, policy=policy)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await REDIS.ping()
    yield
    await REDIS.close()
    await ENGINE.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SETTINGS.session_secret)
app.mount(
    f"{SETTINGS.root_path}/static",
    StaticFiles(directory=str(APP_DIR / "static")),
    name="admin-static",
)


def admin_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin_username"))


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse(f"{SETTINGS.root_path}/login", status_code=302)


def require_admin_api(request: Request) -> None:
    if not admin_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def base_context(request: Request, **kwargs: Any) -> dict[str, Any]:
    context = {
        "request": request,
        "root_path": SETTINGS.root_path,
        "admin_username": request.session.get("admin_username"),
        "model_max_tokens": SETTINGS.model_max_tokens,
        "page_title": kwargs.pop("page_title", "NCO LLM CHAT"),
    }
    context.update(kwargs)
    return context


def normalize_user_payload(payload: UserPayload) -> dict[str, Any]:
    identifier = payload.identifier.strip()
    display_name = payload.display_name.strip() or identifier
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    return {
        "identifier": identifier,
        "display_name": display_name,
        "password": payload.password or "",
        "is_active": payload.is_active,
    }


def normalize_policy_payload(payload: PolicyPayload) -> dict[str, Any]:
    window_start = parse_local_datetime(payload.window_start_local)
    window_end = parse_local_datetime(payload.window_end_local)
    if not window_start or not window_end:
        raise HTTPException(status_code=400, detail="window_start and window_end are required")
    if window_end <= window_start:
        raise HTTPException(status_code=400, detail="window_end must be after window_start")
    return {
        "total_token_limit": payload.total_token_limit,
        "is_active": payload.is_active,
        "window_start": window_start,
        "window_end": window_end,
    }


def sanitize_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "identifier": row.get("identifier"),
        "display_name": row.get("display_name"),
        "is_active": row.get("is_active"),
    }


async def preview_import(csv_text: str) -> dict[str, Any]:
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="csv_text is required")

    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"identifier", "password"}
    headers = set(reader.fieldnames or [])
    missing = required - headers
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing CSV columns: {', '.join(sorted(missing))}",
        )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_row in enumerate(reader, start=2):
        normalized = {key: (value or "").strip() for key, value in raw_row.items()}
        identifier = normalized.get("identifier", "")
        password = normalized.get("password", "")
        display_name = normalized.get("display_name") or identifier
        row_result = {
            "row_number": index,
            "identifier": identifier,
            "action": "created",
            "status": "ok",
            "error_message": None,
            "row_payload": {},
        }
        if not identifier:
            row_result["status"] = "error"
            row_result["error_message"] = "identifier is required"
        elif not password:
            row_result["status"] = "error"
            row_result["error_message"] = "password is required"
        elif identifier in seen:
            row_result["status"] = "error"
            row_result["error_message"] = "duplicate identifier in CSV"
        else:
            existing = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
            row_result["action"] = "updated" if existing else "created"
            row_result["row_payload"] = sanitize_row_payload(
                {
                    "identifier": identifier,
                    "display_name": display_name,
                    "is_active": parse_bool(normalized.get("is_active"), True),
                }
            )
        seen.add(identifier)
        rows.append(row_result)

    success_rows = sum(1 for row in rows if row["status"] == "ok")
    failure_rows = len(rows) - success_rows
    return {
        "rows": rows,
        "total_rows": len(rows),
        "success_rows": success_rows,
        "failure_rows": failure_rows,
    }


async def apply_import_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    success_rows = 0
    failure_rows = 0
    finalized_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = row["row_payload"]
        if row["status"] != "ok":
            failure_rows += 1
            finalized_rows.append(row)
            continue
        try:
            identifier = str(payload["identifier"])
            display_name = str(payload.get("display_name") or identifier)
            is_active = bool(payload.get("is_active", True))
            existing = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
            if existing:
                await REPOSITORY.update_managed_user(
                    identifier,
                    display_name=display_name,
                    password_hash=hash_password("temporary-import-password"),
                    is_active=is_active,
                )
            else:
                await REPOSITORY.create_managed_user(
                    identifier=identifier,
                    display_name=display_name,
                    password_hash=hash_password("temporary-import-password"),
                    is_active=is_active,
                )
            failure_rows += 1
            row["status"] = "error"
            row["error_message"] = "password was not preserved for confirm path"
            finalized_rows.append(row)
        except Exception as exc:  # pragma: no cover - operational path
            failure_rows += 1
            row["status"] = "error"
            row["error_message"] = str(exc)
            finalized_rows.append(row)
        else:
            success_rows += 1
            row["status"] = "success"
            finalized_rows.append(row)
    return {
        "rows": finalized_rows,
        "success_rows": success_rows,
        "failure_rows": failure_rows,
    }


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(SETTINGS.root_path, status_code=302)


@app.get(f"{SETTINGS.root_path}/healthz")
async def healthz() -> JSONResponse:
    db_ok = False
    redis_ok = False
    try:
        async with ENGINE.begin() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    try:
        redis_ok = bool(await REDIS.ping())
    except Exception:
        redis_ok = False

    healthy = db_ok and redis_ok
    return JSONResponse(
        {
            "ok": healthy,
            "database": db_ok,
            "redis": redis_ok,
        },
        status_code=200 if healthy else 503,
    )


@app.get(f"{SETTINGS.root_path}", include_in_schema=False)
async def root_admin(request: Request) -> RedirectResponse:
    if admin_authenticated(request):
        return RedirectResponse(f"{SETTINGS.root_path}/users", status_code=302)
    return RedirectResponse(f"{SETTINGS.root_path}/login", status_code=302)


@app.get(f"{SETTINGS.root_path}/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if admin_authenticated(request):
        return RedirectResponse(f"{SETTINGS.root_path}/users", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        base_context(request, page_title="NCO LLM CHAT Login"),
    )


@app.get(f"{SETTINGS.root_path}/users", response_class=HTMLResponse)
async def users_page(request: Request, edit: str | None = None) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()
    users = await REPOSITORY.list_managed_users()
    edit_user = await REPOSITORY.get_managed_user(edit, include_deleted=True) if edit else None
    edit_usage = None
    if edit:
        try:
            edit_usage = await REPOSITORY.get_user_usage(edit)
        except KeyError:
            edit_usage = None
    return templates.TemplateResponse(
        "users.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Users",
            users=users,
            edit_user=edit_user,
            edit_usage=edit_usage,
        ),
    )


@app.get(f"{SETTINGS.root_path}/quota-policies", response_class=HTMLResponse)
async def quota_policies_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()
    quota_policy = await REPOSITORY.get_global_quota_policy()
    if quota_policy:
        start = (
            datetime.fromisoformat(quota_policy["window_start"])
            if quota_policy.get("window_start")
            else None
        )
        end = (
            datetime.fromisoformat(quota_policy["window_end"])
            if quota_policy.get("window_end")
            else None
        )
        now = datetime.now(timezone.utc)
        quota_policy["active_now"] = bool(
            quota_policy.get("is_active")
            and (start is None or start <= now)
            and (end is None or end >= now)
        )
    return templates.TemplateResponse(
        "quota_policies.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Quota Policies",
            quota_policy=quota_policy,
        ),
    )


@app.post(f"{SETTINGS.root_path}/api/auth/login")
async def login_api(request: Request, payload: LoginPayload) -> JSONResponse:
    admin_account = await REPOSITORY.get_admin_account(payload.username.strip())
    if (
        not admin_account
        or not admin_account.get("is_active")
        or not verify_password(payload.password, admin_account.get("password_hash", ""))
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    request.session["admin_username"] = payload.username.strip()
    return JSONResponse({"ok": True, "username": payload.username.strip()})


@app.post(f"{SETTINGS.root_path}/api/auth/logout")
async def logout_api(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get(f"{SETTINGS.root_path}/api/users")
async def list_users_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    users = await REPOSITORY.list_managed_users()
    return JSONResponse({"items": users})


@app.post(f"{SETTINGS.root_path}/api/users")
async def create_user_api(request: Request, payload: UserPayload) -> JSONResponse:
    require_admin_api(request)
    data = normalize_user_payload(payload)
    existing = await REPOSITORY.get_managed_user(data["identifier"], include_deleted=True)
    if existing and not existing.get("is_deleted"):
        raise HTTPException(status_code=409, detail="User already exists")
    password = data["password"]
    if not password:
        raise HTTPException(status_code=400, detail="password is required")
    await REPOSITORY.create_managed_user(
        identifier=data["identifier"],
        display_name=data["display_name"],
        password_hash=hash_password(password),
        is_active=data["is_active"],
    )
    return JSONResponse({"ok": True, "identifier": data["identifier"]})


@app.get(f"{SETTINGS.root_path}/api/users/{{identifier}}")
async def get_user_api(request: Request, identifier: str) -> JSONResponse:
    require_admin_api(request)
    user = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    usage = await REPOSITORY.get_user_usage(identifier)
    return JSONResponse({"user": user, "usage": usage})


@app.patch(f"{SETTINGS.root_path}/api/users/{{identifier}}")
async def update_user_api(
    request: Request,
    identifier: str,
    payload: UserPayload,
) -> JSONResponse:
    require_admin_api(request)
    data = normalize_user_payload(payload)
    existing = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    password_hash = hash_password(data["password"]) if data["password"] else None
    await REPOSITORY.update_managed_user(
        identifier,
        display_name=data["display_name"],
        password_hash=password_hash,
        is_active=data["is_active"],
    )
    return JSONResponse({"ok": True, "identifier": identifier})


@app.delete(f"{SETTINGS.root_path}/api/users/{{identifier}}")
async def delete_user_api(request: Request, identifier: str) -> JSONResponse:
    require_admin_api(request)
    user = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await REPOSITORY.soft_delete_managed_user(identifier)
    return JSONResponse({"ok": True, "identifier": identifier})


@app.post(f"{SETTINGS.root_path}/api/users/import")
async def import_users_api(request: Request, payload: ImportPayload) -> JSONResponse:
    require_admin_api(request)
    preview = await preview_import(payload.csv_text)
    job_id = await REPOSITORY.create_import_job(
        preview_only=not payload.confirm,
        total_rows=preview["total_rows"],
    )
    await REPOSITORY.replace_import_job_rows(job_id, preview["rows"])
    if not payload.confirm:
        await REPOSITORY.complete_import_job(
            job_id,
            status="preview",
            success_rows=preview["success_rows"],
            failure_rows=preview["failure_rows"],
        )
        return JSONResponse(
            {
                "job_id": job_id,
                "preview_only": True,
                **preview,
            }
        )

    success_rows = 0
    failure_rows = 0
    finalized_rows: list[dict[str, Any]] = []
    for row in preview["rows"]:
        if row["status"] != "ok":
            failure_rows += 1
            finalized_rows.append(row)
            continue
        identifier = str(row["identifier"])
        existing = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
        source = row["row_payload"]
        password = ""
        for parsed_row in csv.DictReader(io.StringIO(payload.csv_text)):
            if (parsed_row.get("identifier") or "").strip() == identifier:
                password = (parsed_row.get("password") or "").strip()
                break
        try:
            if existing:
                await REPOSITORY.update_managed_user(
                    identifier,
                    display_name=str(source.get("display_name") or identifier),
                    password_hash=hash_password(password),
                    is_active=bool(source.get("is_active", True)),
                )
            else:
                await REPOSITORY.create_managed_user(
                    identifier=identifier,
                    display_name=str(source.get("display_name") or identifier),
                    password_hash=hash_password(password),
                    is_active=bool(source.get("is_active", True)),
                )
        except Exception as exc:  # pragma: no cover - operational path
            row["status"] = "error"
            row["error_message"] = str(exc)
            failure_rows += 1
        else:
            row["status"] = "success"
            success_rows += 1
        finalized_rows.append(row)

    await REPOSITORY.replace_import_job_rows(job_id, finalized_rows)
    await REPOSITORY.complete_import_job(
        job_id,
        status="completed",
        success_rows=success_rows,
        failure_rows=failure_rows,
    )
    return JSONResponse(
        {
            "job_id": job_id,
            "preview_only": False,
            "rows": finalized_rows,
            "total_rows": preview["total_rows"],
            "success_rows": success_rows,
            "failure_rows": failure_rows,
        }
    )


@app.get(f"{SETTINGS.root_path}/api/users/{{identifier}}/usage")
async def user_usage_api(request: Request, identifier: str) -> JSONResponse:
    require_admin_api(request)
    try:
        usage = await REPOSITORY.get_user_usage(identifier)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc
    return JSONResponse({"usage": usage})


@app.get(f"{SETTINGS.root_path}/api/quota-policy")
async def get_quota_policy_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    policy = await REPOSITORY.get_global_quota_policy()
    return JSONResponse({"item": policy})


@app.put(f"{SETTINGS.root_path}/api/quota-policy")
async def update_quota_policy_api(request: Request, payload: PolicyPayload) -> JSONResponse:
    require_admin_api(request)
    data = normalize_policy_payload(payload)
    policy = await REPOSITORY.upsert_global_quota_policy(
        window_start=data["window_start"],
        window_end=data["window_end"],
        total_token_limit=data["total_token_limit"],
        is_active=data["is_active"],
    )
    await sync_global_quota_policy_cache(policy)
    return JSONResponse({"ok": True, "id": policy["id"], "item": policy})


@app.get(f"{SETTINGS.root_path}/api/monitoring/summary")
async def monitoring_summary_api(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "implemented": False,
            "detail": "Monitoring is out of scope for this delivery.",
        },
        status_code=501,
    )


@app.get(f"{SETTINGS.root_path}/api/monitoring/alerts")
async def monitoring_alerts_api(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "implemented": False,
            "detail": "Monitoring is out of scope for this delivery.",
            "items": [],
        },
        status_code=501,
    )


@app.get(f"{SETTINGS.root_path}/users/import-template.csv")
async def import_template() -> HTMLResponse:
    content = (
        "identifier,password,display_name,is_active\n"
        "alice,alice-pass,Alice,true\n"
    )
    return HTMLResponse(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="user-import-template.csv"'},
    )
