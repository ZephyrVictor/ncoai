from __future__ import annotations

import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tiktoken
from redis.asyncio import Redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from database import create_app_engine
from monitoring_notifications import (
    SecretCipher,
    build_secret_cipher,
    merge_channel_secret_ciphertext,
    normalize_notification_channel_payload,
    send_test_notification,
)
from ops_service import load_ops_alerts, load_ops_logs, load_ops_summary
from ncoai_shared.bootstrap import (
    bootstrap_team_token_auth_config as bootstrap_team_token_config,
)
from ncoai_shared.bootstrap import sync_global_quota_policy_cache as sync_global_cache
from ncoai_shared.bootstrap import (
    sync_team_token_auth_config_cache as sync_team_token_cache,
)
from ncoai_shared.controlplane import (
    TEAM_TOKEN_IDENTIFIER_PREFIX,
    ControlPlaneRepository,
)
from ncoai_shared.gzctf_team_token import resolve_public_key_base64
from ncoai_shared.monitoring import AppMetrics
from ncoai_shared.security import hash_password, verify_password
from ncoai_shared.team_token_auth import (
    TEAM_TOKEN_AUTH_EVENTS_CHANNEL,
    build_team_token_auth_event,
)
from service_availability import (
    ServiceAvailabilityController,
    load_services_availability,
)
from settings import SETTINGS
from vllm_redundancy import VLLMRedundancyController

LOGGER = logging.getLogger("ncoai.admin")
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
APP_METRICS = AppMetrics("admin")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
VLLM_REDUNDANCY_CONTROLLER: VLLMRedundancyController | None = None
SERVICE_AVAILABILITY_CONTROLLER: ServiceAvailabilityController | None = None
MONITORING_SECRET_CIPHER: SecretCipher = build_secret_cipher(
    SETTINGS.monitoring_secret_key
)


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


def decorate_subject(subject: dict[str, Any] | None) -> dict[str, Any] | None:
    if subject is None:
        return None

    row = dict(subject)
    auth_source = (
        "team-token"
        if str(row.get("identifier", "")).startswith(TEAM_TOKEN_IDENTIFIER_PREFIX)
        else "managed-user"
    )
    row["auth_source"] = auth_source
    row["auth_label"] = "Team Token" if auth_source == "team-token" else "密码"
    row["is_password_editable"] = auth_source != "team-token"
    if not row.get("display_name") and auth_source == "team-token":
        parts = str(row["identifier"]).split(":")
        if len(parts) >= 3 and parts[-1].isdigit():
            row["display_name"] = f"Team #{parts[-1]}"
    return row


async def sync_admin_subjects() -> None:
    await REPOSITORY.sync_team_token_subjects()


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


class TeamTokenAuthPayload(BaseModel):
    public_key: str


class NotificationChannelPayload(BaseModel):
    name: str
    channel_type: str
    is_active: bool = True
    config_public: dict[str, Any] = Field(default_factory=dict)
    config_secret: dict[str, Any] = Field(default_factory=dict)


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


async def sync_team_token_auth_config_cache(
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return await sync_team_token_cache(ENGINE, REDIS, config=config)


async def get_team_token_auth_config() -> dict[str, Any] | None:
    config = await REPOSITORY.get_team_token_auth_config()
    if config is not None:
        return config

    try:
        config = await bootstrap_team_token_config(
            ENGINE,
            public_key_base64=os.getenv("GZCTF_FIXED_GAME_PUBLIC_KEY"),
            private_key_base64=os.getenv("GZCTF_FIXED_GAME_PRIVATE_KEY"),
        )
    except ValueError as exc:
        LOGGER.warning("Failed to bootstrap team token auth config: %s", exc)
        return None
    if config is not None:
        await sync_team_token_auth_config_cache(config)
    return config


def normalize_team_token_auth_payload(payload: TeamTokenAuthPayload) -> dict[str, Any]:
    try:
        public_key = resolve_public_key_base64(payload.public_key, "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"public_key": public_key}


async def publish_team_token_auth_config_update(config: dict[str, Any]) -> None:
    await REDIS.publish(
        TEAM_TOKEN_AUTH_EVENTS_CHANNEL,
        build_team_token_auth_event(config),
    )


def build_vllm_redundancy_controller() -> VLLMRedundancyController:
    return VLLMRedundancyController(
        settings=SETTINGS,
        redis=REDIS,
        metrics=APP_METRICS,
        logger=LOGGER,
    )


def get_vllm_redundancy_controller() -> VLLMRedundancyController:
    global VLLM_REDUNDANCY_CONTROLLER
    if VLLM_REDUNDANCY_CONTROLLER is None:
        VLLM_REDUNDANCY_CONTROLLER = build_vllm_redundancy_controller()
    return VLLM_REDUNDANCY_CONTROLLER


def build_service_availability_controller() -> ServiceAvailabilityController:
    return ServiceAvailabilityController(
        settings=SETTINGS,
        prometheus_url=SETTINGS.monitoring_prometheus_url,
        repository=REPOSITORY,
        redis=REDIS,
        logger=LOGGER,
        secret_cipher=MONITORING_SECRET_CIPHER,
    )


def get_service_availability_controller() -> ServiceAvailabilityController:
    global SERVICE_AVAILABILITY_CONTROLLER
    if SERVICE_AVAILABILITY_CONTROLLER is None:
        SERVICE_AVAILABILITY_CONTROLLER = build_service_availability_controller()
    return SERVICE_AVAILABILITY_CONTROLLER


@asynccontextmanager
async def lifespan(_: FastAPI):
    controller = get_vllm_redundancy_controller()
    service_controller = get_service_availability_controller()
    await REDIS.ping()
    if controller.monitoring_enabled():
        await controller.start()
    await service_controller.start()
    try:
        yield
    finally:
        await service_controller.stop()
        await controller.stop()
        await REDIS.close()
        await ENGINE.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SETTINGS.session_secret)
app.mount(
    f"{SETTINGS.root_path}/static",
    StaticFiles(directory=str(APP_DIR / "static")),
    name="admin-static",
)


@app.middleware("http")
async def instrument_requests(request: Request, call_next) -> Response:
    return await APP_METRICS.instrument_http(request, call_next)


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
        "team_token_fixed_game_id": SETTINGS.team_token_fixed_game_id,
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


@app.get(f"{SETTINGS.root_path}/metrics")
async def metrics() -> Response:
    return APP_METRICS.render_metrics()


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
    await sync_admin_subjects()
    users = await REPOSITORY.list_managed_users()
    edit_user = (
        decorate_subject(await REPOSITORY.get_managed_user(edit, include_deleted=True))
        if edit
        else None
    )
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


@app.get(f"{SETTINGS.root_path}/team-token-auth", response_class=HTMLResponse)
async def team_token_auth_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()
    config = await get_team_token_auth_config()
    return templates.TemplateResponse(
        "team_token_auth.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Team Token Auth",
            team_token_auth_config=config,
        ),
    )


@app.get(f"{SETTINGS.root_path}/services", response_class=HTMLResponse)
async def services_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()
    availability = await load_services_availability(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
        settings=SETTINGS,
    )
    return templates.TemplateResponse(
        "services.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Service Availability",
            services_availability=availability,
        ),
    )


@app.get(f"{SETTINGS.root_path}/monitoring-notifications", response_class=HTMLResponse)
async def monitoring_notifications_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()
    channels = await REPOSITORY.list_monitor_notification_channels()
    return templates.TemplateResponse(
        "monitoring_notifications.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Monitoring Notifications",
            monitoring_channels=channels,
            monitoring_secret_ready=MONITORING_SECRET_CIPHER.configured,
            monitoring_secret_error=MONITORING_SECRET_CIPHER.error,
        ),
    )


@app.get(f"{SETTINGS.root_path}/ops", response_class=HTMLResponse)
async def ops_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()

    summary = await load_ops_summary(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
        grafana_url=SETTINGS.monitoring_grafana_url,
    )
    alerts = await load_ops_alerts(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
    )
    logs = await load_ops_logs(
        loki_url=SETTINGS.monitoring_loki_url,
        limit=30,
    )
    return templates.TemplateResponse(
        "ops.html",
        base_context(
            request,
            page_title="NCO LLM CHAT Ops",
            ops_summary=summary,
            ops_alerts=alerts,
            ops_logs=logs,
            grafana_url=SETTINGS.monitoring_grafana_url,
        ),
    )


@app.get(f"{SETTINGS.root_path}/vllm-redundancy", response_class=HTMLResponse)
async def vllm_redundancy_page(request: Request) -> HTMLResponse:
    if not admin_authenticated(request):
        return redirect_to_login()

    payload = await get_vllm_redundancy_controller().load_payload()
    return templates.TemplateResponse(
        "vllm_redundancy.html",
        base_context(
            request,
            page_title="NCO LLM CHAT vLLM Redundancy",
            vllm_redundancy=payload,
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
        APP_METRICS.record_auth_attempt("admin-password", "failure")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    request.session["admin_username"] = payload.username.strip()
    APP_METRICS.record_auth_attempt("admin-password", "success")
    return JSONResponse({"ok": True, "username": payload.username.strip()})


@app.post(f"{SETTINGS.root_path}/api/auth/logout")
async def logout_api(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get(f"{SETTINGS.root_path}/api/users")
async def list_users_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    await sync_admin_subjects()
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
    await sync_admin_subjects()
    user = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    usage = await REPOSITORY.get_user_usage(identifier)
    return JSONResponse({"user": decorate_subject(user), "usage": usage})


@app.patch(f"{SETTINGS.root_path}/api/users/{{identifier}}")
async def update_user_api(
    request: Request,
    identifier: str,
    payload: UserPayload,
) -> JSONResponse:
    require_admin_api(request)
    await sync_admin_subjects()
    data = normalize_user_payload(payload)
    existing = await REPOSITORY.get_managed_user(identifier, include_deleted=True)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    password_hash = None
    if data["password"] and not identifier.startswith(TEAM_TOKEN_IDENTIFIER_PREFIX):
        password_hash = hash_password(data["password"])
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
    await sync_admin_subjects()
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


@app.get(f"{SETTINGS.root_path}/api/team-token-auth")
async def get_team_token_auth_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    config = await get_team_token_auth_config()
    return JSONResponse({"item": config})


@app.put(f"{SETTINGS.root_path}/api/team-token-auth")
async def update_team_token_auth_api(
    request: Request,
    payload: TeamTokenAuthPayload,
) -> JSONResponse:
    require_admin_api(request)
    data = normalize_team_token_auth_payload(payload)
    config, changed = await REPOSITORY.upsert_team_token_auth_config(
        public_key=data["public_key"],
    )
    await sync_team_token_auth_config_cache(config)
    if changed:
        await publish_team_token_auth_config_update(config)
    return JSONResponse({"ok": True, "changed": changed, "item": config})


@app.get(f"{SETTINGS.root_path}/api/vllm-redundancy")
async def get_vllm_redundancy_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    payload = await get_vllm_redundancy_controller().load_payload()
    return JSONResponse(payload)


@app.post(f"{SETTINGS.root_path}/api/vllm-redundancy/ping")
async def ping_vllm_redundancy_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    payload = await get_vllm_redundancy_controller().refresh_now(
        reason="manual-ping",
        allow_restart=False,
    )
    return JSONResponse(payload)


@app.get(f"{SETTINGS.root_path}/api/services/availability")
async def services_availability_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    payload = await load_services_availability(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
        settings=SETTINGS,
    )
    return JSONResponse(payload)


@app.get(f"{SETTINGS.root_path}/api/monitoring/channels")
async def list_monitoring_channels_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    channels = await REPOSITORY.list_monitor_notification_channels()
    return JSONResponse(
        {
            "items": channels,
            "secret_ready": MONITORING_SECRET_CIPHER.configured,
            "secret_error": MONITORING_SECRET_CIPHER.error,
        }
    )


@app.post(f"{SETTINGS.root_path}/api/monitoring/channels")
async def create_monitoring_channel_api(
    request: Request,
    payload: NotificationChannelPayload,
) -> JSONResponse:
    require_admin_api(request)
    normalized = normalize_notification_channel_payload(
        name=payload.name,
        channel_type=payload.channel_type,
        is_active=payload.is_active,
        config_public=payload.config_public,
        config_secret=payload.config_secret,
    )
    ciphertext = merge_channel_secret_ciphertext(
        normalized_payload=normalized,
        existing_channel=None,
        secret_cipher=MONITORING_SECRET_CIPHER,
    )
    channel = await REPOSITORY.create_monitor_notification_channel(
        name=normalized["name"],
        channel_type=normalized["channel_type"],
        is_active=normalized["is_active"],
        config_public=normalized["config_public"],
        config_secret_ciphertext=ciphertext,
    )
    return JSONResponse({"ok": True, "item": channel})


@app.put(f"{SETTINGS.root_path}/api/monitoring/channels/{{channel_id}}")
async def update_monitoring_channel_api(
    request: Request,
    channel_id: str,
    payload: NotificationChannelPayload,
) -> JSONResponse:
    require_admin_api(request)
    existing = await REPOSITORY.get_monitor_notification_channel(
        channel_id,
        include_secret=True,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Channel not found")
    normalized = normalize_notification_channel_payload(
        name=payload.name,
        channel_type=payload.channel_type,
        is_active=payload.is_active,
        config_public=payload.config_public,
        config_secret=payload.config_secret,
    )
    ciphertext = merge_channel_secret_ciphertext(
        normalized_payload=normalized,
        existing_channel=existing,
        secret_cipher=MONITORING_SECRET_CIPHER,
    )
    channel = await REPOSITORY.update_monitor_notification_channel(
        channel_id,
        name=normalized["name"],
        channel_type=normalized["channel_type"],
        is_active=normalized["is_active"],
        config_public=normalized["config_public"],
        config_secret_ciphertext=ciphertext,
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return JSONResponse({"ok": True, "item": channel})


@app.delete(f"{SETTINGS.root_path}/api/monitoring/channels/{{channel_id}}")
async def delete_monitoring_channel_api(
    request: Request,
    channel_id: str,
) -> JSONResponse:
    require_admin_api(request)
    existing = await REPOSITORY.get_monitor_notification_channel(channel_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Channel not found")
    await REPOSITORY.delete_monitor_notification_channel(channel_id)
    return JSONResponse({"ok": True, "id": channel_id})


@app.post(f"{SETTINGS.root_path}/api/monitoring/channels/{{channel_id}}/test")
async def test_monitoring_channel_api(
    request: Request,
    channel_id: str,
) -> JSONResponse:
    require_admin_api(request)
    channel = await REPOSITORY.get_monitor_notification_channel(
        channel_id,
        include_secret=True,
    )
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    result = await send_test_notification(
        channel,
        secret_cipher=MONITORING_SECRET_CIPHER,
    )
    await REPOSITORY.touch_monitor_notification_channel_test(channel_id)
    return JSONResponse({"ok": bool(result.get("ok")), "result": result})


@app.get(f"{SETTINGS.root_path}/api/ops/summary")
async def ops_summary_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    payload = await load_ops_summary(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
        grafana_url=SETTINGS.monitoring_grafana_url,
    )
    return JSONResponse(payload)


@app.get(f"{SETTINGS.root_path}/api/ops/alerts")
async def ops_alerts_api(request: Request) -> JSONResponse:
    require_admin_api(request)
    payload = await load_ops_alerts(
        prometheus_url=SETTINGS.monitoring_prometheus_url,
    )
    return JSONResponse(payload)


@app.get(f"{SETTINGS.root_path}/api/ops/logs")
async def ops_logs_api(
    request: Request,
    service: str | None = None,
    level: str | None = None,
    q: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> JSONResponse:
    require_admin_api(request)
    payload = await load_ops_logs(
        loki_url=SETTINGS.monitoring_loki_url,
        service=service,
        level=level,
        text_query=q,
        hours=hours,
        limit=limit,
    )
    return JSONResponse(payload)


@app.get(f"{SETTINGS.root_path}/api/monitoring/summary")
async def monitoring_summary_api(request: Request) -> JSONResponse:
    return await ops_summary_api(request)


@app.get(f"{SETTINGS.root_path}/api/monitoring/alerts")
async def monitoring_alerts_api(request: Request) -> JSONResponse:
    return await ops_alerts_api(request)


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
