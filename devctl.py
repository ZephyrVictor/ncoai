#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import selectors
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "infra" / "env" / ".env.dev"
ENV_DIR = ROOT / "infra" / "env"
COMMON_ENV_FILE = ENV_DIR / "common.env"
HOST_ENV_FILE = ENV_DIR / "host.env"
COMPOSE_ENV_FILE = ENV_DIR / "compose.env"
SECRETS_ENV_FILE = ENV_DIR / "secrets.env"
STATE_DIR = ROOT / ".local" / "devctl"
PID_DIR = STATE_DIR / "pids"
LOG_DIR = STATE_DIR / "logs"
TMUX_SESSION = "ncoai-dev"
DEFAULT_START_ORDER = ("vllm", "litellm", "caps-ftp", "chainlit", "admin")
DEFAULT_STOP_ORDER = tuple(reversed(DEFAULT_START_ORDER))
LOG_RETENTION_TIMEZONE = ZoneInfo("Asia/Shanghai")
LOG_RELAY_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    name: str
    script_relpath: str
    final_markers: tuple[str, ...]
    readiness_timeout_seconds: float = 120.0
    alternate_final_markers: tuple[str, ...] = ()

    @property
    def script_path(self) -> Path:
        return ROOT / self.script_relpath

    @property
    def identity_markers(self) -> tuple[str, ...]:
        return (str(self.script_path), *self.final_markers, *self.alternate_final_markers)


SERVICES: dict[str, ServiceDefinition] = {
    "caps-ftp": ServiceDefinition(
        name="caps-ftp",
        script_relpath="scripts/caps/start-local.sh",
        final_markers=("apps/chainlit/caps_service.py",),
        readiness_timeout_seconds=60.0,
    ),
    "vllm": ServiceDefinition(
        name="vllm",
        script_relpath="scripts/vllm/start-local.sh",
        final_markers=("vllm.entrypoints.openai.api_server",),
        alternate_final_markers=(str(ROOT / "scripts/vllm/start_replicas.py"),),
        readiness_timeout_seconds=900.0,
    ),
    "litellm": ServiceDefinition(
        name="litellm",
        script_relpath="scripts/litellm/start-local.sh",
        final_markers=(str(ROOT / ".local" / "generated" / "litellm.config.yaml"), "litellm"),
        readiness_timeout_seconds=180.0,
    ),
    "chainlit": ServiceDefinition(
        name="chainlit",
        script_relpath="scripts/chainlit/start-local.sh",
        final_markers=("apps/chainlit/app.py", "chainlit run"),
        readiness_timeout_seconds=180.0,
    ),
    "admin": ServiceDefinition(
        name="admin",
        script_relpath="scripts/admin/start-local.sh",
        final_markers=("apps/admin", "uvicorn app:app"),
        readiness_timeout_seconds=120.0,
    ),
}
SERVICE_NAMES = tuple(SERVICES)
LOGGABLE_NAMES = SERVICE_NAMES
CHAINLIT_RUNTIME_RE = re.compile(r"^chainlit-(\d+)$")


def layered_env_files(mode: str) -> list[Path]:
    if mode == "host":
        files = [COMMON_ENV_FILE, HOST_ENV_FILE, SECRETS_ENV_FILE]
    elif mode == "compose":
        files = [COMMON_ENV_FILE, COMPOSE_ENV_FILE, SECRETS_ENV_FILE]
    else:
        raise ValueError(f"Unknown env mode: {mode}")

    if all(path.exists() for path in files):
        return files
    return [ENV_FILE]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env(mode: str) -> dict[str, str]:
    env = os.environ.copy()
    for path in layered_env_files(mode):
        env.update(parse_env_file(path))
    return env


def env_source_snippet(mode: str) -> str:
    files = layered_env_files(mode)
    pieces = ["set -a"]
    for path in files:
        pieces.append(f"source {shlex.quote(str(path))}")
    pieces.append("set +a")
    return " && ".join(pieces)


def run_script(path: Path) -> int:
    proc = subprocess.run(
        ["bash", str(path)],
        cwd=ROOT,
        check=False,
        text=True,
    )
    return proc.returncode


def ensure_runtime_dirs() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def service_state_path(runtime_name: str) -> Path:
    return PID_DIR / f"{runtime_name}.json"


def service_log_path(runtime_name: str) -> Path:
    return LOG_DIR / f"{runtime_name}.log"


def service_log_day_path(runtime_name: str) -> Path:
    return LOG_DIR / f"{runtime_name}.day"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def log_day_for_instant(instant: datetime, *, tz: ZoneInfo = LOG_RETENTION_TIMEZONE) -> str:
    return instant.astimezone(tz).date().isoformat()


class DailyLogWriter:
    def __init__(
        self,
        *,
        log_path: Path,
        day_path: Path,
        tz: ZoneInfo = LOG_RETENTION_TIMEZONE,
    ) -> None:
        self.log_path = log_path
        self.day_path = day_path
        self.tz = tz
        self._handle: Any | None = None
        self._current_day: str | None = None

    def _stored_day(self) -> str | None:
        if self._current_day:
            return self._current_day

        if self.day_path.exists():
            value = self.day_path.read_text(encoding="utf-8").strip()
            if value:
                return value

        if self.log_path.exists():
            stat = self.log_path.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            return log_day_for_instant(modified_at, tz=self.tz)

        return None

    def rotate_if_needed(self, instant: datetime | None = None) -> bool:
        now = instant or utcnow()
        current_day = log_day_for_instant(now, tz=self.tz)
        if self._handle is not None and self._current_day == current_day:
            return False

        stored_day = self._stored_day()
        mode = "ab" if stored_day == current_day and self.log_path.exists() else "wb"

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._handle is not None:
            self._handle.close()
        self._handle = self.log_path.open(mode)
        self._current_day = current_day
        self.day_path.write_text(current_day, encoding="utf-8")
        return True

    def write(self, data: bytes, *, instant: datetime | None = None) -> None:
        self.rotate_if_needed(instant)
        assert self._handle is not None
        self._handle.write(data)
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_stdout_bytes(data: bytes) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def normalize_services(services: list[str] | None, order: tuple[str, ...]) -> list[str]:
    if services:
        expanded = [
            name
            for service in services
            for name in (order if service == "apps" else (service,))
        ]
        invalid = [service for service in expanded if not is_known_service_name(service)]
        if invalid:
            raise SystemExit(
                f"[ERROR] Unknown service(s): {', '.join(invalid)}. Choices: {', '.join(SERVICE_NAMES)}"
            )
        return list(dict.fromkeys(expanded))
    return [name for name in order if name in SERVICES]


def chainlit_worker_count(env: dict[str, str] | None = None) -> int:
    values = env or load_env("host")
    return max(int(values.get("CHAINLIT_WORKER_COUNT", "1") or "1"), 1)


def chainlit_worker_base_port(env: dict[str, str] | None = None) -> int:
    values = env or load_env("host")
    default_port = values.get("CHAINLIT_PORT", "3100") or "3100"
    return int(values.get("CHAINLIT_WORKER_BASE_PORT", default_port) or default_port)


def is_chainlit_runtime_name(name: str) -> bool:
    return CHAINLIT_RUNTIME_RE.match(name) is not None


def runtime_service_name(name: str) -> str:
    if is_chainlit_runtime_name(name):
        return "chainlit"
    return name


def is_known_service_name(name: str) -> bool:
    return name in SERVICES or is_chainlit_runtime_name(name)


def chainlit_runtime_names(env: dict[str, str] | None = None) -> list[str]:
    names = {
        f"chainlit-{index}"
        for index in range(1, chainlit_worker_count(env) + 1)
    }
    if PID_DIR.exists():
        for path in PID_DIR.glob("chainlit-*.json"):
            if is_chainlit_runtime_name(path.stem):
                names.add(path.stem)
    for window_name in list_windows():
        if is_chainlit_runtime_name(window_name):
            names.add(window_name)
    return sorted(names, key=lambda name: chainlit_runtime_index(name))


def runtime_names_for_service(service: str, env: dict[str, str] | None = None) -> list[str]:
    if service == "chainlit":
        return chainlit_runtime_names(env)
    return [service]


def runtime_display_name(name: str) -> str:
    return name


def chainlit_runtime_index(name: str) -> int:
    match = CHAINLIT_RUNTIME_RE.match(name)
    if not match:
        raise ValueError(f"Not a Chainlit runtime name: {name}")
    return int(match.group(1))


def env_overrides_for_runtime(
    runtime_name: str,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    if not is_chainlit_runtime_name(runtime_name):
        return {}
    values = env or load_env("host")
    worker_index = chainlit_runtime_index(runtime_name)
    port = chainlit_worker_base_port(values) + worker_index - 1
    return {
        "CHAINLIT_WORKER_ID": runtime_name,
        "CHAINLIT_WORKER_INDEX": str(worker_index),
        "CHAINLIT_PORT": str(port),
    }


def read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="ignore").strip()


def pid_matches_service(pid: int, service: ServiceDefinition) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False

    cmdline = read_cmdline(pid)
    return str(service.script_path) in cmdline or all(
        marker in cmdline for marker in service.final_markers
    ) or bool(service.alternate_final_markers) and all(
        marker in cmdline for marker in service.alternate_final_markers
    )


def discover_service_pids(service: ServiceDefinition) -> list[int]:
    proc = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=True,
        text=True,
        capture_output=True,
    )
    current_pid = os.getpid()
    current_ppid = os.getppid()
    matches: list[int] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_str, _, args = line.partition(" ")
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid in {current_pid, current_ppid}:
            continue
        if all(marker in args for marker in service.final_markers) or (
            service.alternate_final_markers and all(marker in args for marker in service.alternate_final_markers)
        ):
            matches.append(pid)
    return matches


def cleanup_stale_state(service: str) -> None:
    path = service_state_path(service)
    if path.exists():
        path.unlink()


def load_running_state(runtime_name: str) -> dict[str, Any] | None:
    state = read_json(service_state_path(runtime_name))
    if not state:
        return None
    service = runtime_service_name(runtime_name)
    pid = int(state.get("pid", 0) or 0)
    if pid_matches_service(pid, SERVICES[service]):
        return state
    cleanup_stale_state(runtime_name)
    return None


def get_unmanaged_pids(service: str) -> list[int]:
    env = load_env("host")
    managed_pids = {
        int(state["pid"])
        for runtime_name in runtime_names_for_service(service, env)
        if (state := load_running_state(runtime_name)) is not None
    }
    pids: list[int] = []
    for pid in discover_service_pids(SERVICES[service]):
        if pid in managed_pids:
            continue
        pids.append(pid)
    return pids


def tail_text(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        content = handle.readlines()
    return "".join(content[-lines:])


def service_readiness_urls(
    service: str,
    env: dict[str, str],
    *,
    env_overrides: dict[str, str] | None = None,
) -> list[str]:
    merged_env = dict(env)
    if env_overrides:
        merged_env.update(env_overrides)
    if service == "vllm":
        from ncoai_shared.vllm_replicas import replica_environments
        return [f"http://127.0.0.1:{replica.get('VLLM_PORT', '12233')}{path}"
                for replica in replica_environments(merged_env)
                for path in ("/health", "/v1/models")]
    if service == "litellm":
        port = merged_env.get("LITELLM_PORT", "33455")
        return [
            f"http://127.0.0.1:{port}/health/readiness",
            f"http://127.0.0.1:{port}/v1/models",
        ]
    if service == "chainlit":
        port = merged_env.get("CHAINLIT_PORT", "3100")
        root_path = merged_env.get("CHAINLIT_ROOT_PATH", "/chat").rstrip("/")
        return [f"http://127.0.0.1:{port}{root_path}/health"]
    if service == "admin":
        port = merged_env.get("ADMIN_APP_PORT", "3200")
        root_path = merged_env.get("ADMIN_APP_ROOT_PATH", "/admin").rstrip("/")
        return [f"http://127.0.0.1:{port}{root_path}/healthz"]
    return []


def should_wait_for_readiness(env: dict[str, str] | None = None) -> bool:
    values = env if env is not None else load_env("host")
    raw = values.get("DEVCTL_WAIT_FOR_READINESS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def wait_for_service_readiness(
    service: str,
    *,
    env_overrides: dict[str, str] | None = None,
) -> bool:
    env = load_env("host")
    merged_env = dict(env)
    if env_overrides:
        merged_env.update(env_overrides)
    if service == "caps-ftp":
        host = merged_env.get("CAPS_FTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(merged_env.get("CAPS_FTP_PORT", "3221") or "3221")
        deadline = time.time() + SERVICES[service].readiness_timeout_seconds
        last_error = ""
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=3):
                    return True
            except OSError as exc:
                last_error = str(exc)
            time.sleep(1.0)
        if last_error:
            print(f"[ERROR] {service} readiness check failed: {last_error}", file=sys.stderr)
        return False
    urls = service_readiness_urls(service, env, env_overrides=env_overrides)
    if not urls:
        return True

    deadline = time.time() + SERVICES[service].readiness_timeout_seconds
    last_error = ""
    headers = {}
    if service == "litellm" and merged_env.get("LITELLM_MASTER_KEY"):
        headers["Authorization"] = f"Bearer {merged_env['LITELLM_MASTER_KEY']}"
    elif (
        service == "vllm"
        and merged_env.get("VLLM_API_KEY")
        and merged_env.get("VLLM_API_KEY") != "dummy"
    ):
        headers["Authorization"] = f"Bearer {merged_env['VLLM_API_KEY']}"
    require_all_urls = service == "vllm"
    while time.time() < deadline:
        ready_count = 0
        for url in urls:
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    if 200 <= response.status < 300:
                        if not require_all_urls:
                            return True
                        ready_count += 1
                    else:
                        last_error = f"{url} returned {response.status}"
            except urllib.error.URLError as exc:
                last_error = f"{url}: {exc}"
            except Exception as exc:  # pragma: no cover - operational path
                last_error = f"{url}: {exc}"
        if require_all_urls and ready_count == len(urls):
            return True
        time.sleep(1.0)

    if last_error:
        print(f"[ERROR] {service} readiness check failed: {last_error}", file=sys.stderr)
    return False


def stop_process_group(pid: int, pgid: int | None, timeout_seconds: float = 20.0) -> None:
    targets: list[tuple[str, int]] = []
    if pgid and pgid > 0:
        targets.append(("pgid", pgid))
    targets.append(("pid", pid))

    for label, target in targets:
        try:
            if label == "pgid":
                os.killpg(target, signal.SIGTERM)
            else:
                os.kill(target, signal.SIGTERM)
            break
        except ProcessLookupError:
            return
        except PermissionError:
            continue

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)

    for label, target in targets:
        try:
            if label == "pgid":
                os.killpg(target, signal.SIGKILL)
            else:
                os.kill(target, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            continue


def has_tmux() -> bool:
    proc = subprocess.run(["tmux", "-V"], capture_output=True, text=True)
    return proc.returncode == 0


def tmux_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, text=True, capture_output=True)


def session_exists() -> bool:
    return tmux_run(["has-session", "-t", TMUX_SESSION], check=False).returncode == 0


def list_windows() -> dict[str, dict[str, str]]:
    if not session_exists():
        return {}
    proc = tmux_run(
        ["list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}|#{window_id}|#{window_dead}"],
        check=False,
    )
    if proc.returncode != 0:
        return {}
    windows: dict[str, dict[str, str]] = {}
    for line in proc.stdout.splitlines():
        name, _, rest = line.partition("|")
        window_id, _, dead = rest.partition("|")
        if name:
            windows[name] = {"window_id": window_id, "dead": dead}
    return windows


def kill_window(service: str) -> None:
    windows = list_windows()
    if service not in windows:
        return
    tmux_run(["kill-window", "-t", f"{TMUX_SESSION}:{service}"], check=False)


def cleanup_empty_session() -> None:
    windows = list_windows()
    if windows:
        return
    tmux_run(["kill-session", "-t", TMUX_SESSION], check=False)


def record_start(
    service: str,
    runtime_name: str,
    pid: int,
    pgid: int,
    tmux_target: str | None,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    ensure_runtime_dirs()
    payload = {
        "service": service,
        "runtime_name": runtime_name,
        "pid": pid,
        "pgid": pgid,
        "log_path": str(service_log_path(runtime_name)),
        "script": str(SERVICES[service].script_path),
        "tmux_target": tmux_target,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if extra:
        payload.update(extra)
    write_json(service_state_path(runtime_name), payload)


def run_service_with_log_relay(
    service: str,
    tmux_target: str | None,
    runtime_name: str | None = None,
    *,
    poll_interval_seconds: float = LOG_RELAY_POLL_INTERVAL_SECONDS,
) -> int:
    ensure_runtime_dirs()
    definition = SERVICES[service]
    runtime = runtime_name or service
    writer = DailyLogWriter(
        log_path=service_log_path(runtime),
        day_path=service_log_day_path(runtime),
    )
    writer.rotate_if_needed()

    proc = subprocess.Popen(
        ["bash", str(definition.script_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    extra: dict[str, Any] = {}
    if service == "chainlit":
        extra["port"] = os.getenv("CHAINLIT_PORT", "3100")
        extra["worker_id"] = os.getenv("CHAINLIT_WORKER_ID", runtime)
    record_start(
        service,
        runtime,
        proc.pid,
        proc.pid,
        tmux_target,
        extra=extra,
    )

    start_line = (
        f"\n=== START {runtime} "
        f"{datetime.now(LOG_RETENTION_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S %z')} ===\n"
    ).encode("utf-8")
    write_stdout_bytes(start_line)
    writer.write(start_line)

    stdout = proc.stdout
    if stdout is None:
        writer.close()
        return proc.wait()

    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ)
    stdout_open = True
    original_handlers: dict[int, Any] = {}

    def forward_signal(signum: int, _frame: Any) -> None:
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            return

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            original_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward_signal)
        except (OSError, RuntimeError, ValueError):
            continue

    try:
        while stdout_open or proc.poll() is None:
            writer.rotate_if_needed()
            events = selector.select(timeout=poll_interval_seconds if stdout_open else 0.0)
            if not events:
                continue

            for key, _mask in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    stdout_open = False
                    continue
                write_stdout_bytes(chunk)
                writer.write(chunk)
    finally:
        selector.close()
        stdout.close()
        writer.close()
        for signum, handler in original_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError):
                continue

    return proc.wait()


def shell_export_snippet(env_overrides: dict[str, str]) -> str:
    if not env_overrides:
        return "true"
    return " && ".join(
        f"export {key}={shlex.quote(value)}" for key, value in env_overrides.items()
    )


def build_tmux_command(service: str, runtime_name: str, env_overrides: dict[str, str]) -> str:
    state_path = service_state_path(runtime_name)
    tmux_target = f"{TMUX_SESSION}:{runtime_name}"
    return "bash -lc " + shlex.quote(
        " && ".join(
            [
                f"cd {shlex.quote(str(ROOT))}",
                f"mkdir -p {shlex.quote(str(PID_DIR))} {shlex.quote(str(LOG_DIR))}",
                env_source_snippet("host"),
                shell_export_snippet(env_overrides),
                "source .venv/bin/activate",
                f"export PYTHONPATH={shlex.quote(str(ROOT))}:${{PYTHONPATH:-}}",
                f"rm -f {shlex.quote(str(state_path))}",
                (
                    f"exec python3 {shlex.quote(str(ROOT / 'devctl.py'))} _run-service "
                    f"{shlex.quote(service)} {shlex.quote(tmux_target)} {shlex.quote(runtime_name)}"
                ),
            ]
        )
    )


def start_runtime(service: str, runtime_name: str, env: dict[str, str]) -> int:
    current = load_running_state(runtime_name)
    if current:
        print(
            f"[INFO] {runtime_display_name(runtime_name)} already running "
            f"(pid={current['pid']}, tmux={current.get('tmux_target') or '-'})"
        )
        return 0

    windows = list_windows()
    if runtime_name in windows:
        kill_window(runtime_name)

    env_overrides = env_overrides_for_runtime(runtime_name, env)
    command = build_tmux_command(service, runtime_name, env_overrides)
    if not session_exists():
        tmux_run(["new-session", "-d", "-s", TMUX_SESSION, "-n", runtime_name, command])
        tmux_run(["set-option", "-t", TMUX_SESSION, "remain-on-exit", "on"])
    else:
        tmux_run(["new-window", "-d", "-t", TMUX_SESSION, "-n", runtime_name, command])
        tmux_run(
            ["set-window-option", "-t", f"{TMUX_SESSION}:{runtime_name}", "remain-on-exit", "on"],
            check=False,
        )

    deadline = time.time() + 10.0
    while time.time() < deadline:
        state = load_running_state(runtime_name)
        if state:
            if not should_wait_for_readiness(env):
                print(
                    f"[OK] Started {runtime_display_name(runtime_name)} "
                    f"(pid={state['pid']}, tmux={state.get('tmux_target') or '-'})"
                )
                return 0
            if wait_for_service_readiness(service, env_overrides=env_overrides):
                print(
                    f"[OK] Started {runtime_display_name(runtime_name)} "
                    f"(pid={state['pid']}, tmux={state.get('tmux_target') or '-'})"
                )
                return 0
            print(
                f"[ERROR] {runtime_display_name(runtime_name)} process started but did not become ready.",
                file=sys.stderr,
            )
            stop_runtime(service, runtime_name)
            break
        time.sleep(0.2)

    print(
        f"[ERROR] Timed out waiting for {runtime_display_name(runtime_name)} to start.",
        file=sys.stderr,
    )
    log_excerpt = tail_text(service_log_path(runtime_name), lines=80)
    if log_excerpt:
        print(log_excerpt, file=sys.stderr)
    return 1


def start_service(service: str) -> int:
    env = load_env("host")
    if is_chainlit_runtime_name(service):
        return start_runtime("chainlit", service, env)

    unmanaged = get_unmanaged_pids(service)
    if unmanaged:
        print(f"[INFO] {service} already running unmanaged (pid={unmanaged[0]})")
        return 0

    current = load_running_state(service)
    if current:
        print(
            f"[INFO] {service} already running "
            f"(pid={current['pid']}, tmux={current.get('tmux_target') or '-'})"
        )
        return 0

    exit_code = 0
    for runtime_name in runtime_names_for_service(service, env):
        exit_code = max(exit_code, start_runtime(service, runtime_name, env))
        if exit_code != 0:
            break
    return exit_code


def stop_runtime(service: str, runtime_name: str) -> int:
    definition = SERVICES[service]
    state = read_json(service_state_path(runtime_name))
    stopped_any = False

    if state:
        pid = int(state.get("pid", 0) or 0)
        pgid = int(state.get("pgid", 0) or 0)
        if pid_matches_service(pid, definition):
            stop_process_group(pid, pgid)
            stopped_any = True
        cleanup_stale_state(runtime_name)

    kill_window(runtime_name)
    cleanup_empty_session()

    if stopped_any:
        print(f"[OK] Stopped {runtime_display_name(runtime_name)}")
    else:
        print(f"[INFO] {runtime_display_name(runtime_name)} not running")
    return 0


def stop_service(service: str) -> int:
    if is_chainlit_runtime_name(service):
        return stop_runtime("chainlit", service)

    if service == "chainlit":
        env = load_env("host")
        for runtime_name in runtime_names_for_service(service, env):
            stop_runtime(service, runtime_name)
        return 0

    definition = SERVICES[service]
    state = read_json(service_state_path(service))
    stopped_any = False

    if state:
        pid = int(state.get("pid", 0) or 0)
        pgid = int(state.get("pgid", 0) or 0)
        if pid_matches_service(pid, definition):
            stop_process_group(pid, pgid)
            stopped_any = True
        cleanup_stale_state(service)

    unmanaged = get_unmanaged_pids(service)
    for pid in unmanaged:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        stop_process_group(pid, pgid)
        stopped_any = True

    kill_window(service)
    cleanup_empty_session()

    if stopped_any:
        print(f"[OK] Stopped {service}")
    else:
        print(f"[INFO] {service} not running")
    return 0


def status_runtime(service: str, runtime_name: str) -> int:
    state = load_running_state(runtime_name)
    unmanaged = get_unmanaged_pids(service)
    windows = list_windows()
    if state:
        print(
            f"{runtime_display_name(runtime_name)}: running pid={state['pid']} pgid={state['pgid']} "
            f"tmux={state.get('tmux_target') or '-'} log={state['log_path']}"
        )
        return 0
    if runtime_name in windows:
        print(
            f"{runtime_display_name(runtime_name)}: tmux-window-present dead={windows[runtime_name]['dead']}"
        )
        return 0
    if unmanaged and service != "chainlit":
        joined = ", ".join(str(pid) for pid in unmanaged)
        print(f"{runtime_display_name(runtime_name)}: running unmanaged pid={joined}")
        return 0
    print(f"{runtime_display_name(runtime_name)}: stopped")
    return 0


def status_service(service: str) -> int:
    if is_chainlit_runtime_name(service):
        return status_runtime("chainlit", service)
    if service == "chainlit":
        env = load_env("host")
        for runtime_name in runtime_names_for_service(service, env):
            status_runtime(service, runtime_name)
        return 0
    return status_runtime(service, service)


def print_logs(runtime_name: str) -> int:
    log_path = service_log_path(runtime_name)
    if not log_path.exists():
        if session_exists() and runtime_name in list_windows():
            proc = tmux_run(
                ["capture-pane", "-p", "-t", f"{TMUX_SESSION}:{runtime_name}"],
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                print(proc.stdout, end="")
                return 0
        print(f"[INFO] No log file for {runtime_display_name(runtime_name)}: {log_path}")
        return 0
    print(tail_text(log_path), end="")
    return 0


def doctor() -> int:
    env = load_env("host")
    exit_code = 0
    checks: list[tuple[str, bool, str]] = [
        ("tmux", has_tmux(), "required for host-service management"),
        ("docker", shutil.which("docker") is not None, "required for compose infra"),
        ("uv", shutil.which("uv") is not None, "required for host init"),
        (
            "model_path",
            bool(env.get("MODEL_PATH")) and Path(env["MODEL_PATH"]).exists(),
            env.get("MODEL_PATH", "<unset>"),
        ),
        (
            "venv",
            (ROOT / ".venv" / "bin" / "python").exists(),
            str(ROOT / ".venv"),
        ),
    ]

    for name, ok, detail in checks:
        print(f"{name}: {'ok' if ok else 'missing'} ({detail})")
        if not ok:
            exit_code = 1

    return exit_code


def init() -> int:
    for script_name in ("init-host.sh", "init-infra.sh", "init-db.sh"):
        path = ROOT / "scripts" / "deploy" / script_name
        code = run_script(path)
        if code != 0:
            return code
    return 0


def preflight_service_checks(services: list[str]) -> int:
    python_path = ROOT / ".venv" / "bin" / "python"
    if not python_path.exists():
        print("[ERROR] Missing root virtualenv: .venv", file=sys.stderr)
        print("Run: bash scripts/deploy/init-host.sh", file=sys.stderr)
        return 1

    env = load_env("host")
    checks: list[tuple[str, list[str]]] = []
    normalized = {runtime_service_name(service) for service in services}
    if "litellm" in normalized:
        checks.append(("litellm", ["--require-db"]))
    if "admin" in normalized:
        checks.append(("admin", ["--require-db", "--require-redis"]))
    if "chainlit" in normalized:
        checks.append(("chainlit", ["--require-db", "--require-redis"]))

    for service, extra_args in checks:
        proc = subprocess.run(
            [
                str(python_path),
                str(ROOT / "scripts" / "deploy" / "check_runtime.py"),
                "--service",
                service,
                *extra_args,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            if proc.stdout:
                print(proc.stdout, end="", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr)
            return proc.returncode
    return 0


def warmup_vllm(env: dict[str, str] | None = None) -> int:
    values = env if env is not None else load_env("host")
    if values.get("VLLM_WARMUP_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        print("[INFO] vLLM warmup disabled by VLLM_WARMUP_ENABLED=0")
        return 0
    print("[INFO] Waiting for vLLM model and warming shared prefixes", flush=True)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "vllm" / "warmup.py")],
        cwd=ROOT,
        env=values,
        check=False,
    ).returncode


def start(services: list[str] | None = None) -> int:
    ensure_runtime_dirs()
    selected = normalize_services(services, DEFAULT_START_ORDER)
    if (not services or "apps" in services) and load_env("host").get("CAPS_ENABLED") != "1":
        selected = [service for service in selected if service != "caps-ftp"]
    exit_code = 0
    if selected:
        if not has_tmux():
            print("[ERROR] tmux not found. Please install tmux first.", file=sys.stderr)
            return 1
        if preflight_service_checks(selected) != 0:
            return 1
    vllm_warmed = False
    for service in selected:
        if service != "vllm" and runtime_service_name(service) in {"litellm", "chainlit"} and not vllm_warmed:
            if warmup_vllm() != 0:
                return 1
            vllm_warmed = True
        exit_code = max(exit_code, start_service(service))
        if exit_code != 0:
            break
        if service == "vllm" and not vllm_warmed:
            if warmup_vllm() != 0:
                return 1
            vllm_warmed = True
    if exit_code == 0:
        print(f"[INFO] Attach with: tmux attach -t {TMUX_SESSION}")
    return exit_code


def stop(services: list[str] | None = None) -> int:
    ensure_runtime_dirs()
    selected = normalize_services(services, DEFAULT_STOP_ORDER)
    ordered = [name for name in DEFAULT_STOP_ORDER if name in selected]
    ordered.extend(
        name for name in selected if name not in ordered and is_chainlit_runtime_name(name)
    )
    for service in ordered:
        stop_service(service)
    return 0


def status(services: list[str] | None = None) -> int:
    ensure_runtime_dirs()
    selected = normalize_services(services, DEFAULT_START_ORDER)
    if session_exists():
        print(f"[INFO] tmux session: {TMUX_SESSION}")
    else:
        print(f"[INFO] tmux session: {TMUX_SESSION} (not running)")
    for service in selected:
        status_service(service)
    return 0


def logs(service: str) -> int:
    ensure_runtime_dirs()
    if is_chainlit_runtime_name(service):
        return print_logs(service)
    if service == "chainlit":
        env = load_env("host")
        runtime_names = runtime_names_for_service(service, env)
        for index, runtime_name in enumerate(runtime_names):
            if index:
                print()
            print(f"=== {runtime_display_name(runtime_name)} ===")
            print_logs(runtime_name)
        return 0
    return print_logs(service)


def attach() -> int:
    if not has_tmux():
        print("[ERROR] tmux not found.", file=sys.stderr)
        return 1
    if not session_exists():
        print(f"[INFO] tmux session not running: {TMUX_SESSION}")
        return 0
    return subprocess.run(["tmux", "attach", "-t", TMUX_SESSION], check=False).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ncoai local service controller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Run host, infra, and database initialization")

    p_up = sub.add_parser("up", help="Start managed local services")
    p_up.add_argument("services", nargs="*")

    p_down = sub.add_parser("down", help="Stop managed local services")
    p_down.add_argument("services", nargs="*")

    p_status = sub.add_parser("status", help="Show managed service status")
    p_status.add_argument("services", nargs="*")

    p_logs = sub.add_parser("logs", help="Show service logs")
    p_logs.add_argument("service")

    sub.add_parser("attach", help="Attach to the tmux service session")
    sub.add_parser("doctor", help="Run deployment prerequisite checks")
    sub.add_parser("warmup", help="Wait for and warm the configured vLLM model")

    p_record = sub.add_parser("_record-start", help=argparse.SUPPRESS)
    p_record.add_argument("service")
    p_record.add_argument("runtime_name")
    p_record.add_argument("pid", type=int)
    p_record.add_argument("pgid", type=int)
    p_record.add_argument("tmux_target")

    p_run_service = sub.add_parser("_run-service", help=argparse.SUPPRESS)
    p_run_service.add_argument("service")
    p_run_service.add_argument("tmux_target")
    p_run_service.add_argument("runtime_name")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cmd == "init":
        return init()
    if args.cmd == "up":
        return start(args.services)
    if args.cmd == "down":
        return stop(args.services)
    if args.cmd == "status":
        return status(args.services)
    if args.cmd == "logs":
        return logs(args.service)
    if args.cmd == "attach":
        return attach()
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "warmup":
        return warmup_vllm()
    if args.cmd == "_record-start":
        record_start(
            args.service,
            args.runtime_name,
            args.pid,
            args.pgid,
            args.tmux_target,
        )
        return 0
    if args.cmd == "_run-service":
        return run_service_with_log_relay(
            args.service,
            args.tmux_target,
            args.runtime_name,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
