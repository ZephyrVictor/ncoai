from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import devctl


def test_daily_log_writer_rotates_to_next_day_and_keeps_only_current_day(tmp_path) -> None:
    writer = devctl.DailyLogWriter(
        log_path=tmp_path / "admin.log",
        day_path=tmp_path / "admin.day",
    )
    first_day = datetime(2026, 4, 8, 1, 0, tzinfo=timezone.utc)
    next_day = first_day + timedelta(days=1)

    writer.write(b"first\n", instant=first_day)
    writer.write(b"second\n", instant=first_day + timedelta(hours=2))
    writer.rotate_if_needed(next_day)
    writer.write(b"third\n", instant=next_day)
    writer.close()

    assert (tmp_path / "admin.log").read_text(encoding="utf-8") == "third\n"
    assert (tmp_path / "admin.day").read_text(encoding="utf-8").strip() == "2026-04-09"


def test_daily_log_writer_clears_previous_day_without_new_output(tmp_path) -> None:
    writer = devctl.DailyLogWriter(
        log_path=tmp_path / "admin.log",
        day_path=tmp_path / "admin.day",
    )
    first_day = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    next_day = first_day + timedelta(days=1)

    writer.write(b"stale\n", instant=first_day)
    writer.rotate_if_needed(next_day)
    writer.close()

    assert (tmp_path / "admin.log").read_text(encoding="utf-8") == ""
    assert (tmp_path / "admin.day").read_text(encoding="utf-8").strip() == "2026-04-09"


def test_run_service_with_log_relay_records_real_child_pid(tmp_path, monkeypatch) -> None:
    script_path = tmp_path / "dummy-service.sh"
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "echo $$\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    monkeypatch.setattr(devctl, "PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(devctl, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        devctl,
        "SERVICES",
        {
            **devctl.SERVICES,
            "dummy": devctl.ServiceDefinition(
                name="dummy",
                script_relpath=str(script_path),
                final_markers=("dummy-service.sh",),
            ),
        },
    )

    exit_code = devctl.run_service_with_log_relay(
        "dummy",
        "ncoai-dev:dummy",
        poll_interval_seconds=0.01,
    )

    assert exit_code == 0

    state = json.loads((tmp_path / "pids" / "dummy.json").read_text(encoding="utf-8"))
    log_lines = (tmp_path / "logs" / "dummy.log").read_text(encoding="utf-8").splitlines()
    child_pid = next(int(line.strip()) for line in log_lines if line.strip().isdigit())

    assert state["pid"] == child_pid
    assert state["pgid"] == child_pid
