#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys

SCRIPTS = [
    "scripts/admin_e2e_users.py",
    "scripts/admin_e2e_import.py",
    "scripts/admin_e2e_quota_v2.py",
    "scripts/admin_e2e_monitoring.py",
    "scripts/chat_e2e_stream_resume_copy.py",
    "scripts/chat_e2e_feature_flags.py",
]


def main() -> int:
    report: dict[str, int] = {}
    exit_code = 0
    for script in SCRIPTS:
        proc = subprocess.run([sys.executable, script], check=False)
        report[script] = proc.returncode
        exit_code = max(exit_code, proc.returncode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
