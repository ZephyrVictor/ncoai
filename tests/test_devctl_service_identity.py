from __future__ import annotations

import devctl


def test_litellm_pid_matches_generated_runtime_config(monkeypatch) -> None:
    monkeypatch.setattr(devctl.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        devctl,
        "read_cmdline",
        lambda pid: (
            f"/home/ncoai/workspace/llmchat/ncoai/.venv/bin/python3 "
            f"/home/ncoai/workspace/llmchat/ncoai/.venv/bin/litellm "
            f"--host 127.0.0.1 --port 33455 --config "
            f"{devctl.ROOT / '.local' / 'generated' / 'litellm.config.yaml'}"
        ),
    )

    assert devctl.pid_matches_service(12345, devctl.SERVICES["litellm"])
