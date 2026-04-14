from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from ncoai_shared.monitoring import AppMetrics
from ncoai_shared.vllm_failover import (
    LiteLLMFailoverSettings,
    VLLMEndpoint,
    build_litellm_failover_settings,
    build_local_origin,
    probe_vllm_endpoint,
    serialize_datetime,
    utcnow,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
STATE_REDIS_KEY = "vllm-redundancy:state:global"
EVENTS_REDIS_KEY = "vllm-redundancy:events:global"

ProbeFunc = Callable[[VLLMEndpoint, float], Awaitable[dict[str, Any]]]
ProcessReader = Callable[[], dict[str, Any]]
CommandRunner = Callable[[list[str], float], Awaitable[dict[str, Any]]]
NowFunc = Callable[[], datetime]


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def default_probe_func(
    endpoint: VLLMEndpoint,
    timeout_seconds: float,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        probe_vllm_endpoint,
        endpoint,
        timeout_seconds=timeout_seconds,
    )


def default_process_reader() -> dict[str, Any]:
    try:
        import devctl
    except Exception as exc:  # pragma: no cover - operational import failure
        return {
            "running": False,
            "managed": False,
            "pid": None,
            "source": "unavailable",
            "detail": str(exc),
        }

    managed_state = devctl.load_running_state("vllm")
    unmanaged_pids = devctl.get_unmanaged_pids("vllm")
    if managed_state:
        return {
            "running": True,
            "managed": True,
            "pid": int(managed_state.get("pid") or 0),
            "source": "managed",
            "detail": "",
        }
    if unmanaged_pids:
        return {
            "running": True,
            "managed": False,
            "pid": int(unmanaged_pids[0]),
            "source": "unmanaged",
            "detail": "",
        }
    return {
        "running": False,
        "managed": False,
        "pid": None,
        "source": "missing",
        "detail": "No vLLM process is running.",
    }


async def default_command_runner(
    args: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT_DIR / "devctl.py"), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        output = stdout.decode("utf-8", errors="replace")
        return {
            "ok": proc.returncode == 0,
            "returncode": int(proc.returncode or 0),
            "command": cmd,
            "output": output,
            "timed_out": False,
        }
    except asyncio.TimeoutError:
        proc.kill()
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        return {
            "ok": False,
            "returncode": 124,
            "command": cmd,
            "output": output,
            "timed_out": True,
        }


class VLLMRedundancyController:
    def __init__(
        self,
        *,
        settings: Any,
        redis: Redis,
        metrics: AppMetrics,
        logger: logging.Logger,
        probe_func: ProbeFunc = default_probe_func,
        process_reader: ProcessReader = default_process_reader,
        command_runner: CommandRunner = default_command_runner,
        now_func: NowFunc = utcnow,
        event_limit: int = 20,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.metrics = metrics
        self.logger = logger
        self.probe_func = probe_func
        self.process_reader = process_reader
        self.command_runner = command_runner
        self.now_func = now_func
        self.event_limit = event_limit
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._state_cache: dict[str, Any] | None = None
        self._restart_attempts: list[datetime] = []

    def monitoring_enabled(self) -> bool:
        return bool(self.settings.vllm_failover_enabled)

    def _config_env(self) -> dict[str, str]:
        return {
            "VLLM_FAILOVER_ENABLED": "1" if self.settings.vllm_failover_enabled else "0",
            "VLLM_BACKUP_ORIGIN": self.settings.vllm_backup_origin,
            "VLLM_HOST": self.settings.vllm_host,
            "VLLM_PORT": str(self.settings.vllm_port),
            "SERVED_MODEL_NAME": self.settings.served_model_name,
            "VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS": str(
                self.settings.vllm_primary_request_timeout_seconds
            ),
            "VLLM_BACKUP_REQUEST_TIMEOUT_SECONDS": str(
                self.settings.vllm_backup_request_timeout_seconds
            ),
        }

    def _load_config(self) -> tuple[LiteLLMFailoverSettings | None, str | None]:
        try:
            return build_litellm_failover_settings(self._config_env()), None
        except ValueError as exc:
            return None, str(exc)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.refresh_now(reason="startup", allow_restart=False)
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self.refresh_now(reason="monitor", allow_restart=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - operational path
                self.logger.warning("vLLM redundancy monitor refresh failed: %s", exc)
            await asyncio.sleep(
                float(self.settings.vllm_redundancy_probe_interval_seconds)
            )

    async def refresh_now(
        self,
        *,
        reason: str,
        allow_restart: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            previous = self._state_cache or await self._read_state()
            if previous:
                self._restore_restart_attempts(previous)
            state = await self._refresh_locked(
                previous_state=previous,
                reason=reason,
                allow_restart=allow_restart,
            )
            self._state_cache = state
            await self._write_state(state)
            return {**state, "events": await self._read_events()}

    async def load_payload(self, *, refresh_if_missing: bool = True) -> dict[str, Any]:
        if self._state_cache is None:
            state = await self._read_state()
            if state is not None:
                self._state_cache = state
                self._restore_restart_attempts(state)
        if self._state_cache is None and refresh_if_missing:
            return await self.refresh_now(reason="page", allow_restart=False)
        return {
            **(self._state_cache or self._empty_state()),
            "events": await self._read_events(),
        }

    def _restore_restart_attempts(self, state: dict[str, Any]) -> None:
        attempts = []
        for raw in state.get("recent_restart_attempts", []):
            parsed = _parse_iso_datetime(str(raw))
            if parsed is not None:
                attempts.append(parsed)
        self._restart_attempts = attempts

    def _empty_state(self) -> dict[str, Any]:
        config, config_error = self._load_config()
        primary_origin = (
            config.primary_endpoint.origin
            if config is not None
            else build_local_origin(self.settings.vllm_host, self.settings.vllm_port)
        )
        primary_api_base = (
            config.primary_endpoint.api_base if config is not None else f"{primary_origin}/v1"
        )
        backup_origin = (
            config.backup_endpoint.origin
            if config is not None and config.backup_endpoint is not None
            else str(self.settings.vllm_backup_origin or "").strip()
        )
        backup_api_base = (
            config.backup_endpoint.api_base
            if config is not None and config.backup_endpoint is not None
            else (f"{backup_origin.rstrip('/')}/v1" if backup_origin else "")
        )
        return {
            "generated_at": serialize_datetime(self.now_func()),
            "mode": "disabled" if not self.settings.vllm_failover_enabled else "degraded",
            "config_error": config_error or "",
            "config": {
                "failover_enabled": bool(self.settings.vllm_failover_enabled),
                "primary_origin": primary_origin,
                "primary_api_base": primary_api_base,
                "backup_origin": backup_origin,
                "backup_api_base": backup_api_base,
                "primary_request_timeout_seconds": float(
                    self.settings.vllm_primary_request_timeout_seconds
                ),
                "backup_request_timeout_seconds": float(
                    self.settings.vllm_backup_request_timeout_seconds
                ),
            },
            "primary": {
                "origin": primary_origin,
                "api_base": primary_api_base,
                "health_url": f"{primary_origin}/health",
                "models_url": f"{primary_origin}/v1/models",
                "healthy": False,
                "process_running": False,
                "managed": False,
                "pid": None,
                "checked_at": None,
                "last_error": config_error or "",
            },
            "backup": {
                "configured": bool(self.settings.vllm_failover_enabled and backup_origin),
                "origin": backup_origin,
                "api_base": backup_api_base,
                "health_url": f"{backup_origin}/health" if backup_origin else "",
                "models_url": f"{backup_origin}/v1/models" if backup_origin else "",
                "healthy": None,
                "checked_at": None,
                "last_error": config_error or "",
            },
            "last_restart": {
                "status": "never",
                "reason": "",
                "attempted_at": None,
                "completed_at": None,
                "detail": "",
            },
            "restart_budget": {
                "max_attempts": int(self.settings.vllm_redundancy_restart_max_attempts),
                "window_seconds": int(
                    self.settings.vllm_redundancy_restart_window_seconds
                ),
                "cooldown_seconds": int(
                    self.settings.vllm_redundancy_restart_cooldown_seconds
                ),
                "attempts_in_window": 0,
                "exhausted": False,
                "next_allowed_at": None,
            },
            "recent_restart_attempts": [],
        }

    async def _refresh_locked(
        self,
        *,
        previous_state: dict[str, Any] | None,
        reason: str,
        allow_restart: bool,
    ) -> dict[str, Any]:
        now = self.now_func()
        config, config_error = self._load_config()
        primary_origin = build_local_origin(self.settings.vllm_host, self.settings.vllm_port)
        primary_endpoint = (
            config.primary_endpoint
            if config is not None
            else VLLMEndpoint(
                origin=primary_origin,
                api_base=f"{primary_origin}/v1",
                health_url=f"{primary_origin}/health",
                models_url=f"{primary_origin}/v1/models",
            )
        )
        primary_probe = await self.probe_func(
            primary_endpoint,
            float(self.settings.vllm_redundancy_probe_timeout_seconds),
        )
        process_status = await asyncio.to_thread(self.process_reader)

        backup_probe: dict[str, Any] | None = None
        if config is not None and config.backup_endpoint is not None:
            backup_probe = await self.probe_func(
                config.backup_endpoint,
                float(self.settings.vllm_redundancy_probe_timeout_seconds),
            )

        previous_last_restart = (
            dict(previous_state.get("last_restart", {}))
            if previous_state
            else {
                "status": "never",
                "reason": "",
                "attempted_at": None,
                "completed_at": None,
                "detail": "",
            }
        )
        restart_result = previous_last_restart
        if allow_restart and config_error is None:
            restart_result = await self._maybe_restart_primary(
                now=now,
                previous_last_restart=previous_last_restart,
                primary_probe=primary_probe,
                process_status=process_status,
            )
            if restart_result.get("status") == "success":
                primary_probe = await self.probe_func(
                    primary_endpoint,
                    float(self.settings.vllm_redundancy_probe_timeout_seconds),
                )
                process_status = await asyncio.to_thread(self.process_reader)

        budget = self._build_restart_budget(now)
        primary_healthy = bool(primary_probe.get("healthy")) and bool(
            process_status.get("running")
        )
        backup_healthy = bool(backup_probe and backup_probe.get("healthy"))

        if config_error:
            mode = "degraded"
        elif not self.settings.vllm_failover_enabled:
            mode = "disabled" if primary_healthy else "degraded"
        elif primary_healthy:
            mode = "primary"
        elif backup_healthy:
            mode = "failed_over"
        else:
            mode = "degraded"

        backup_origin = (
            config.backup_endpoint.origin
            if config is not None and config.backup_endpoint is not None
            else str(self.settings.vllm_backup_origin or "").strip()
        )
        backup_api_base = (
            config.backup_endpoint.api_base
            if config is not None and config.backup_endpoint is not None
            else ""
        )

        state = {
            "generated_at": serialize_datetime(now),
            "mode": mode,
            "config_error": config_error or "",
            "config": {
                "failover_enabled": bool(self.settings.vllm_failover_enabled),
                "primary_origin": primary_endpoint.origin,
                "primary_api_base": primary_endpoint.api_base,
                "backup_origin": backup_origin,
                "backup_api_base": backup_api_base,
                "primary_request_timeout_seconds": float(
                    self.settings.vllm_primary_request_timeout_seconds
                ),
                "backup_request_timeout_seconds": float(
                    self.settings.vllm_backup_request_timeout_seconds
                ),
            },
            "primary": {
                "origin": primary_endpoint.origin,
                "api_base": primary_endpoint.api_base,
                "health_url": primary_endpoint.health_url,
                "models_url": primary_endpoint.models_url,
                "healthy": primary_healthy,
                "process_running": bool(process_status.get("running")),
                "managed": bool(process_status.get("managed")),
                "pid": process_status.get("pid"),
                "checked_at": primary_probe.get("checked_at"),
                "last_error": self._primary_error_message(primary_probe, process_status),
            },
            "backup": {
                "configured": bool(self.settings.vllm_failover_enabled and backup_origin),
                "origin": backup_origin,
                "api_base": backup_api_base,
                "health_url": (
                    config.backup_endpoint.health_url
                    if config is not None and config.backup_endpoint is not None
                    else ""
                ),
                "models_url": (
                    config.backup_endpoint.models_url
                    if config is not None and config.backup_endpoint is not None
                    else ""
                ),
                "healthy": backup_probe.get("healthy") if backup_probe is not None else None,
                "checked_at": backup_probe.get("checked_at") if backup_probe else None,
                "last_error": backup_probe.get("error") if backup_probe else "",
            },
            "last_restart": restart_result,
            "restart_budget": budget,
            "recent_restart_attempts": [
                serialize_datetime(item) for item in self._restart_attempts
            ],
        }

        await self._emit_transition_events(previous_state, state, reason=reason)
        self.metrics.update_vllm_redundancy_state(
            service="vllm",
            enabled=bool(self.settings.vllm_failover_enabled),
            mode=mode,
            primary_healthy=bool(state["primary"]["healthy"]),
            primary_process_running=bool(state["primary"]["process_running"]),
            backup_healthy=state["backup"]["healthy"],
            restart_budget_exhausted=bool(budget["exhausted"]),
        )
        return state

    def _primary_error_message(
        self,
        primary_probe: dict[str, Any],
        process_status: dict[str, Any],
    ) -> str:
        errors: list[str] = []
        if not process_status.get("running"):
            detail = str(process_status.get("detail") or "").strip()
            errors.append(detail or "vLLM process is not running.")
        if not primary_probe.get("healthy"):
            detail = str(primary_probe.get("error") or "").strip()
            errors.append(detail or "Primary vLLM health probe failed.")
        return "; ".join(item for item in errors if item)

    def _trim_restart_attempts(self, now: datetime) -> None:
        window = timedelta(seconds=int(self.settings.vllm_redundancy_restart_window_seconds))
        self._restart_attempts = [
            item for item in self._restart_attempts if now - item <= window
        ]

    def _build_restart_budget(self, now: datetime) -> dict[str, Any]:
        self._trim_restart_attempts(now)
        attempts_in_window = len(self._restart_attempts)
        max_attempts = int(self.settings.vllm_redundancy_restart_max_attempts)
        cooldown = timedelta(
            seconds=int(self.settings.vllm_redundancy_restart_cooldown_seconds)
        )
        next_allowed: datetime | None = None
        if self._restart_attempts:
            next_allowed = self._restart_attempts[-1] + cooldown
        exhausted = attempts_in_window >= max_attempts
        if exhausted and self._restart_attempts:
            window_end = self._restart_attempts[0] + timedelta(
                seconds=int(self.settings.vllm_redundancy_restart_window_seconds)
            )
            next_allowed = max(next_allowed or window_end, window_end)
        return {
            "max_attempts": max_attempts,
            "window_seconds": int(self.settings.vllm_redundancy_restart_window_seconds),
            "cooldown_seconds": int(self.settings.vllm_redundancy_restart_cooldown_seconds),
            "attempts_in_window": attempts_in_window,
            "exhausted": exhausted,
            "next_allowed_at": serialize_datetime(next_allowed) if next_allowed else None,
        }

    async def _maybe_restart_primary(
        self,
        *,
        now: datetime,
        previous_last_restart: dict[str, Any],
        primary_probe: dict[str, Any],
        process_status: dict[str, Any],
    ) -> dict[str, Any]:
        primary_unhealthy = (not primary_probe.get("healthy")) or (
            not process_status.get("running")
        )
        if not primary_unhealthy:
            return previous_last_restart

        self._trim_restart_attempts(now)
        max_attempts = int(self.settings.vllm_redundancy_restart_max_attempts)
        cooldown_seconds = int(self.settings.vllm_redundancy_restart_cooldown_seconds)
        if self._restart_attempts:
            last_attempt = self._restart_attempts[-1]
            if (now - last_attempt).total_seconds() < cooldown_seconds:
                return previous_last_restart
        if len(self._restart_attempts) >= max_attempts:
            current = dict(previous_last_restart)
            current["status"] = "budget_exhausted"
            current["reason"] = "restart budget exhausted"
            current["completed_at"] = serialize_datetime(now)
            current["detail"] = "Automatic restarts are temporarily throttled."
            return current

        self._restart_attempts.append(now)
        attempted_at = serialize_datetime(now)
        timeout_seconds = float(self.settings.vllm_redundancy_restart_command_timeout_seconds)
        down_result = await self.command_runner(["down", "vllm"], timeout_seconds)
        up_result = await self.command_runner(["up", "vllm"], timeout_seconds)
        detail = "\n".join(
            item
            for item in (
                str(down_result.get("output") or "").strip(),
                str(up_result.get("output") or "").strip(),
            )
            if item
        )
        succeeded = bool(up_result.get("ok"))
        result = {
            "status": "success" if succeeded else "failure",
            "reason": "primary unhealthy",
            "attempted_at": attempted_at,
            "completed_at": serialize_datetime(self.now_func()),
            "detail": detail,
        }

        await self._append_event(
            level="info" if succeeded else "error",
            event_type="restart_success" if succeeded else "restart_failure",
            message=(
                "Auto-restarted the local vLLM process."
                if succeeded
                else "Automatic vLLM restart failed."
            ),
            details={
                "attempted_at": attempted_at,
                "detail": detail,
            },
        )
        self.metrics.record_vllm_redundancy_restart_attempt(
            service="vllm",
            result="success" if succeeded else "failure",
        )
        if succeeded:
            self.logger.warning("Local vLLM was unhealthy; auto-restart completed.")
        else:
            self.logger.error("Local vLLM restart failed: %s", detail or "unknown error")
        return result

    async def _emit_transition_events(
        self,
        previous_state: dict[str, Any] | None,
        state: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        previous_mode = (
            str(previous_state.get("mode") or "").strip() if previous_state else ""
        )
        next_mode = str(state.get("mode") or "").strip()
        if previous_mode == next_mode:
            return
        if not previous_mode and next_mode in {"primary", "disabled"}:
            return

        if next_mode == "failed_over":
            await self._append_event(
                level="warning",
                event_type="failover_activated",
                message="Local vLLM is unhealthy; requests should fall back to the backup node.",
                details={"reason": reason},
            )
            self.metrics.record_vllm_redundancy_transition(
                service="vllm",
                event="failover_activated",
            )
            self.logger.warning(
                "vLLM redundancy switched to backup mode because the local node is unhealthy."
            )
        elif next_mode == "primary":
            await self._append_event(
                level="info",
                event_type="primary_recovered",
                message="Local vLLM is healthy again; new requests will prefer the primary node.",
                details={"reason": reason},
            )
            self.metrics.record_vllm_redundancy_transition(
                service="vllm",
                event="primary_recovered",
            )
            self.logger.info("vLLM redundancy recovered to the local primary node.")
        elif next_mode == "degraded":
            await self._append_event(
                level="error",
                event_type="degraded",
                message="vLLM redundancy is degraded; the local node is unhealthy and no healthy backup is available.",
                details={"reason": reason, "config_error": state.get("config_error") or ""},
            )
            self.metrics.record_vllm_redundancy_transition(
                service="vllm",
                event="degraded",
            )
            self.logger.error("vLLM redundancy is degraded.")

    async def _read_state(self) -> dict[str, Any] | None:
        getter = getattr(self.redis, "get", None)
        if not callable(getter):
            return None
        raw = await getter(STATE_REDIS_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def _write_state(self, state: dict[str, Any]) -> None:
        setter = getattr(self.redis, "set", None)
        if not callable(setter):
            return
        await setter(STATE_REDIS_KEY, json.dumps(state, ensure_ascii=True))

    async def _read_events(self) -> list[dict[str, Any]]:
        reader = getattr(self.redis, "lrange", None)
        if not callable(reader):
            return []
        items = await reader(EVENTS_REDIS_KEY, 0, self.event_limit - 1)
        events: list[dict[str, Any]] = []
        for item in items:
            try:
                events.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return events

    async def _append_event(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "timestamp": serialize_datetime(self.now_func()),
            "level": level,
            "event": event_type,
            "message": message,
            "details": details or {},
        }
        writer = getattr(self.redis, "lpush", None)
        trimmer = getattr(self.redis, "ltrim", None)
        if callable(writer):
            await writer(EVENTS_REDIS_KEY, json.dumps(entry, ensure_ascii=True))
            if callable(trimmer):
                await trimmer(EVENTS_REDIS_KEY, 0, self.event_limit - 1)
