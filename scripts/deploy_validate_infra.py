#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess

CONTAINERS = {
    "ncoai-postgres": True,
    "ncoai-redis": True,
    "ncoai-nginx": True,
    "ncoai-prometheus": True,
    "ncoai-grafana": True,
    "ncoai-node-exporter": False,
    "ncoai-postgres-exporter": False,
    "ncoai-redis-exporter": False,
    "ncoai-blackbox-exporter": False,
}


def inspect_container(name: str) -> dict[str, str]:
    proc = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"status": "missing", "health": "missing"}
    status, health = (proc.stdout.strip() or "\t").split("\t", 1)
    return {"status": status, "health": health}


def main() -> int:
    report: dict[str, dict[str, str]] = {}
    exit_code = 0
    for name, needs_health in CONTAINERS.items():
        state = inspect_container(name)
        report[name] = state
        if state["status"] != "running":
            exit_code = 1
        if needs_health and state["health"] not in {"healthy", "none"}:
            exit_code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
