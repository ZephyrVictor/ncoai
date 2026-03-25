#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def main() -> int:
    nginx_port = env("NGINX_PORT", "8088")
    env_vars = os.environ.copy()
    env_vars.setdefault("ADMIN_BASE_URL", f"http://127.0.0.1:{nginx_port}/admin")
    env_vars.setdefault("CHAT_BASE_URL", f"http://127.0.0.1:{nginx_port}/chat")
    proc = subprocess.run(
        [sys.executable, "scripts/chat_e2e_stream_resume_copy.py"],
        check=False,
        env=env_vars,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
