#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
DEFAULT_START_ORDER = ("vllm", "litellm", "chainlit", "admin")
DEFAULT_STOP_ORDER = tuple(reversed(DEFAULT_START_ORDER))


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    name: str
    script_relpath: str
    final_markers: tuple[str, ...]
    readiness_timeout_seconds: float = 120.0

    @property
    def script_path(self) -> Path:
        return ROOT / self.script_relpath

    @property
    def identity_markers(self) -> tuple[str, ...]:
        return (str(self.script_path), *self.final_markers)


SERVICES: dict[str, ServiceDefinition] = {
    "vllm": ServiceDefinition(
        name="vllm",
        script_relpath="scripts/vllm/start-qwen35b.sh",
        final_markers=("vllm.entrypoints.openai.api_server",),
        readiness_timeout_seconds=900.0,
    ),
    "litellm": ServiceDefinition(
        name="litellm",
        script_relpath="scripts/litellm/start-local.sh",
        final_markers=("infra/litellm/config.yaml", "litellm"),
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


def service_state_path(service: str) -> Path:
    return PID_DIR / f"{service}.json"


def service_log_path(service: str) -> Path:
    return LOG_DIR / f"{service}.log"


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
        invalid = [service for service in services if service not in SERVICES]
        if invalid:
            raise SystemExit(
                f"[ERROR] Unknown service(s): {', '.join(invalid)}. Choices: {', '.join(SERVICE_NAMES)}"
            )
        return services
    return [name for name in order if name in SERVICES]


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
        if all(marker in args for marker in service.final_markers):
            matches.append(pid)
    return matches


def cleanup_stale_state(service: str) -> None:
    path = service_state_path(service)
    if path.exists():
        path.unlink()


def load_running_state(service: str) -> dict[str, Any] | None:
    state = read_json(service_state_path(service))
    if not state:
        return None
    pid = int(state.get("pid", 0) or 0)
    if pid_matches_service(pid, SERVICES[service]):
        return state
    cleanup_stale_state(service)
    return None


def get_unmanaged_pids(service: str) -> list[int]:
    managed_state = load_running_state(service)
    managed_pid = int(managed_state["pid"]) if managed_state else None
    pids: list[int] = []
    for pid in discover_service_pids(SERVICES[service]):
        if managed_pid and pid == managed_pid:
            continue
        pids.append(pid)
    return pids


def tail_text(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        content = handle.readlines()
    return "".join(content[-lines:])


def service_readiness_urls(service: str, env: dict[str, str]) -> list[str]:
    if service == "vllm":
        port = env.get("VLLM_PORT", "12233")
        return [
            f"http://127.0.0.1:{port}/health",
            f"http://127.0.0.1:{port}/v1/models",
        ]
    if service == "litellm":
        port = env.get("LITELLM_PORT", "33455")
        return [
            f"http://127.0.0.1:{port}/health/liveliness",
            f"http://127.0.0.1:{port}/metrics",
        ]
    if service == "chainlit":
        port = env.get("CHAINLIT_PORT", "3100")
        root_path = env.get("CHAINLIT_ROOT_PATH", "/chat").rstrip("/")
        return [f"http://127.0.0.1:{port}{root_path}/health"]
    if service == "admin":
        port = env.get("ADMIN_APP_PORT", "3200")
        root_path = env.get("ADMIN_APP_ROOT_PATH", "/admin").rstrip("/")
        return [f"http://127.0.0.1:{port}{root_path}/healthz"]
    return []


def wait_for_service_readiness(service: str) -> bool:
    env = load_env("host")
    urls = service_readiness_urls(service, env)
    if not urls:
        return True

    deadline = time.time() + SERVICES[service].readiness_timeout_seconds
    last_error = ""
    while time.time() < deadline:
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if 200 <= response.status < 500:
                        return True
            except urllib.error.URLError as exc:
                last_error = str(exc)
            except Exception as exc:  # pragma: no cover - operational path
                last_error = str(exc)
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


def record_start(service: str, pid: int, pgid: int, tmux_target: str | None) -> None:
    ensure_runtime_dirs()
    payload = {
        "service": service,
        "pid": pid,
        "pgid": pgid,
        "log_path": str(service_log_path(service)),
        "script": str(SERVICES[service].script_path),
        "tmux_target": tmux_target,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(service_state_path(service), payload)


def build_tmux_command(service: str) -> str:
    definition = SERVICES[service]
    log_path = service_log_path(service)
    state_path = service_state_path(service)
    tmux_target = f"{TMUX_SESSION}:{service}"
    return "bash -lc " + shlex.quote(
        " && ".join(
            [
                f"cd {shlex.quote(str(ROOT))}",
                f"mkdir -p {shlex.quote(str(PID_DIR))} {shlex.quote(str(LOG_DIR))}",
                env_source_snippet("host"),
                "source .venv/bin/activate",
                f"export PYTHONPATH={shlex.quote(str(ROOT))}:${{PYTHONPATH:-}}",
                f"exec > >(tee -a {shlex.quote(str(log_path))}) 2>&1",
                f"echo; printf '=== START {service} %s ===\\n' \"$(date '+%Y-%m-%d %H:%M:%S')\"",
                f"rm -f {shlex.quote(str(state_path))}",
                (
                    f"python3 {shlex.quote(str(ROOT / 'devctl.py'))} _record-start "
                    f"{shlex.quote(service)} $$ \"$(ps -o pgid= $$ | tr -d ' ')\" "
                    f"{shlex.quote(tmux_target)}"
                ),
                f"exec bash {shlex.quote(str(definition.script_path))}",
            ]
        )
    )


def start_service(service: str) -> int:
    current = load_running_state(service)
    if current:
        print(
            f"[INFO] {service} already running "
            f"(pid={current['pid']}, tmux={current.get('tmux_target') or '-'})"
        )
        return 0

    unmanaged = get_unmanaged_pids(service)
    if unmanaged:
        print(f"[INFO] {service} already running unmanaged (pid={unmanaged[0]})")
        return 0

    windows = list_windows()
    if service in windows:
        kill_window(service)

    command = build_tmux_command(service)
    if not session_exists():
        tmux_run(["new-session", "-d", "-s", TMUX_SESSION, "-n", service, command])
        tmux_run(["set-option", "-t", TMUX_SESSION, "remain-on-exit", "on"])
    else:
        tmux_run(["new-window", "-d", "-t", TMUX_SESSION, "-n", service, command])
        tmux_run(
            ["set-window-option", "-t", f"{TMUX_SESSION}:{service}", "remain-on-exit", "on"],
            check=False,
        )

    deadline = time.time() + 10.0
    while time.time() < deadline:
        state = load_running_state(service)
        if state:
            if wait_for_service_readiness(service):
                print(
                    f"[OK] Started {service} "
                    f"(pid={state['pid']}, tmux={state.get('tmux_target') or '-'})"
                )
                return 0
            print(f"[ERROR] {service} process started but did not become ready.", file=sys.stderr)
            stop_service(service)
            break
        time.sleep(0.2)

    print(f"[ERROR] Timed out waiting for {service} to start.", file=sys.stderr)
    log_excerpt = tail_text(service_log_path(service), lines=80)
    if log_excerpt:
        print(log_excerpt, file=sys.stderr)
    return 1


def stop_service(service: str) -> int:
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


def status_service(service: str) -> int:
    state = load_running_state(service)
    unmanaged = get_unmanaged_pids(service)
    windows = list_windows()
    if state:
        print(
            f"{service}: running pid={state['pid']} pgid={state['pgid']} "
            f"tmux={state.get('tmux_target') or '-'} log={state['log_path']}"
        )
        return 0
    if unmanaged:
        joined = ", ".join(str(pid) for pid in unmanaged)
        print(f"{service}: running unmanaged pid={joined}")
        return 0
    if service in windows:
        print(f"{service}: tmux-window-present dead={windows[service]['dead']}")
        return 0
    print(f"{service}: stopped")
    return 0


def print_logs(service: str) -> int:
    if session_exists() and service in list_windows():
        proc = tmux_run(["capture-pane", "-p", "-t", f"{TMUX_SESSION}:{service}"], check=False)
        if proc.returncode == 0 and proc.stdout:
            print(proc.stdout, end="")
            return 0
    log_path = service_log_path(service)
    if not log_path.exists():
        print(f"[INFO] No log file for {service}: {log_path}")
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
    if "litellm" in services:
        checks.append(("litellm", ["--require-db"]))
    if "admin" in services:
        checks.append(("admin", ["--require-db", "--require-redis"]))
    if "chainlit" in services:
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


def start(services: list[str] | None = None) -> int:
    ensure_runtime_dirs()
    selected = normalize_services(services, DEFAULT_START_ORDER)
    exit_code = 0
    if selected:
        if not has_tmux():
            print("[ERROR] tmux not found. Please install tmux first.", file=sys.stderr)
            return 1
        if preflight_service_checks(selected) != 0:
            return 1
    for service in selected:
        exit_code = max(exit_code, start_service(service))
        if exit_code != 0:
            break
    if exit_code == 0:
        print(f"[INFO] Attach with: tmux attach -t {TMUX_SESSION}")
    return exit_code


def stop(services: list[str] | None = None) -> int:
    ensure_runtime_dirs()
    selected = normalize_services(services, DEFAULT_STOP_ORDER)
    ordered = [name for name in DEFAULT_STOP_ORDER if name in selected]
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
    p_logs.add_argument("service", choices=LOGGABLE_NAMES)

    sub.add_parser("attach", help="Attach to the tmux service session")
    sub.add_parser("doctor", help="Run deployment prerequisite checks")

    p_record = sub.add_parser("_record-start", help=argparse.SUPPRESS)
    p_record.add_argument("service", choices=SERVICE_NAMES)
    p_record.add_argument("pid", type=int)
    p_record.add_argument("pgid", type=int)
    p_record.add_argument("tmux_target")

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
    if args.cmd == "_record-start":
        record_start(args.service, args.pid, args.pgid, args.tmux_target)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
