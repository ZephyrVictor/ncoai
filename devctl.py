#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSION = "ncoai-dev"

SERVICES = {
    "vllm": "bash scripts/vllm/start-qwen35b.sh",
    "litellm": "bash scripts/litellm/start-local.sh",
    "webui": "bash scripts/openwebui/start-local.sh",
}


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def has_tmux() -> bool:
    try:
        run(["tmux", "-V"])
        return True
    except Exception:
        return False


def session_exists() -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", SESSION],
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0


def tmux_cmd(service: str) -> str:
    # Keep process alive for debugging when command exits.
    return (
        f"cd {ROOT} && "
        "set -a && source infra/env/.env.dev && set +a && "
        "source .venv/bin/activate && "
        f"{SERVICES[service]}; "
        "code=$?; "
        "echo; printf 'EXIT code=%s\\n' \"$code\"; "
        "echo 'INFO Press Enter to close this pane'; read"
    )


def build_launch_command(service: str) -> str:
    return "bash -lc " + shlex.quote(tmux_cmd(service))


def start() -> int:
    if not has_tmux():
        print("[ERROR] tmux not found. Please install tmux first.", file=sys.stderr)
        return 1

    if not (ROOT / ".venv" / "bin" / "python").exists():
        print("[ERROR] Missing root virtualenv: .venv", file=sys.stderr)
        print("Create it with: uv venv .venv --python 3.11", file=sys.stderr)
        return 1

    if session_exists():
        print(f"[INFO] tmux session already exists: {SESSION}")
        print(f"Attach with: tmux attach -t {SESSION}")
        return 0

    first = True
    for name in SERVICES:
        if first:
            run(["tmux", "new-session", "-d", "-s", SESSION, "-n", name])
            run(["tmux", "send-keys", "-t", f"{SESSION}:{name}", build_launch_command(name), "C-m"])
            first = False
        else:
            run(["tmux", "new-window", "-t", SESSION, "-n", name])
            run(["tmux", "send-keys", "-t", f"{SESSION}:{name}", build_launch_command(name), "C-m"])

    print(f"[OK] Started session: {SESSION}")
    print(f"Attach with: tmux attach -t {SESSION}")
    return 0


def stop() -> int:
    if not has_tmux():
        print("[ERROR] tmux not found.", file=sys.stderr)
        return 1
    if not session_exists():
        print(f"[INFO] Session not running: {SESSION}")
        return 0
    run(["tmux", "kill-session", "-t", SESSION])
    print(f"[OK] Stopped session: {SESSION}")
    return 0


def status() -> int:
    if not has_tmux():
        print("[ERROR] tmux not found.", file=sys.stderr)
        return 1
    if not session_exists():
        print(f"[INFO] Session not running: {SESSION}")
        return 0

    proc = run(["tmux", "list-windows", "-t", SESSION, "-F", "#{window_index}:#{window_name}:#{window_active}"])
    print(f"[INFO] Session: {SESSION}")
    print(proc.stdout.strip())
    return 0


def logs(service: str) -> int:
    if not has_tmux():
        print("[ERROR] tmux not found.", file=sys.stderr)
        return 1
    if not session_exists():
        print(f"[INFO] Session not running: {SESSION}")
        return 0
    if service not in SERVICES:
        print(f"[ERROR] Unknown service: {service}", file=sys.stderr)
        return 1

    target = f"{SESSION}:{service}"
    proc = run(["tmux", "capture-pane", "-p", "-t", target], check=False)
    if proc.returncode != 0:
        print(f"[ERROR] Failed to capture logs from {target}", file=sys.stderr)
        return 1
    print(proc.stdout)
    return 0


def attach() -> int:
    if not has_tmux():
        print("[ERROR] tmux not found.", file=sys.stderr)
        return 1
    if not session_exists():
        print(f"[INFO] Session not running: {SESSION}")
        return 0
    subprocess.run(["tmux", "attach", "-t", SESSION], check=False)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ncoai local stack controller (tmux)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("up", help="Start all local services in tmux")
    sub.add_parser("down", help="Stop all local services")
    sub.add_parser("status", help="Show tmux session status")
    sub.add_parser("attach", help="Attach to tmux session")

    p_logs = sub.add_parser("logs", help="Print captured pane output for a service")
    p_logs.add_argument("service", choices=sorted(SERVICES.keys()))

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cmd == "up":
        return start()
    if args.cmd == "down":
        return stop()
    if args.cmd == "status":
        return status()
    if args.cmd == "logs":
        return logs(args.service)
    if args.cmd == "attach":
        return attach()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
