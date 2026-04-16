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


def test_chainlit_runtime_env_overrides(monkeypatch) -> None:
    monkeypatch.setattr(devctl, "list_windows", lambda: {})
    monkeypatch.setattr(
        devctl,
        "load_env",
        lambda mode: {
            "CHAINLIT_WORKER_COUNT": "4",
            "CHAINLIT_WORKER_BASE_PORT": "3100",
            "CHAINLIT_PORT": "3100",
        },
    )

    assert devctl.chainlit_runtime_names() == [
        "chainlit-1",
        "chainlit-2",
        "chainlit-3",
        "chainlit-4",
    ]
    assert devctl.env_overrides_for_runtime("chainlit-3") == {
        "CHAINLIT_WORKER_ID": "chainlit-3",
        "CHAINLIT_WORKER_INDEX": "3",
        "CHAINLIT_PORT": "3102",
    }
