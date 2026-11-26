from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ncoai_shared.vllm_failover import (
    build_litellm_failover_settings,
    render_litellm_config,
)
from ncoai_shared.vllm_replicas import (
    all_vllm_origins,
    local_replica_origins,
    remote_replica_origins,
)
from scripts.vllm.start_remote_vllm import build_command, config_from_args, build_parser


def test_vllm_replicas_local_and_remote_origins():
    env = {
        "VLLM_HOST": "0.0.0.0",
        "VLLM_PORT": "12233",
        "TP_SIZE": "2",
        "VLLM_REPLICA_GPU_GROUPS": "0,1;2,3",
        "VLLM_REMOTE_ORIGINS": "202.112.51.208:18000, http://202.112.51.208:18001/v1",
    }
    assert local_replica_origins(env) == [
        "http://127.0.0.1:12233",
        "http://127.0.0.1:12234",
    ]
    assert remote_replica_origins(env) == [
        "http://202.112.51.208:18000",
        "http://202.112.51.208:18001",
    ]
    assert all_vllm_origins(env) == [
        "http://127.0.0.1:12233",
        "http://127.0.0.1:12234",
        "http://202.112.51.208:18000",
        "http://202.112.51.208:18001",
    ]


def test_litellm_config_prefers_nginx_lb_and_disables_thinking_explicitly():
    template = Path(ROOT / "infra/litellm/config.yaml").read_text(encoding="utf-8")
    env = {
        "SERVED_MODEL_NAME": "qwen38",
        "CHAINLIT_DEFAULT_MODEL": "qwen38_no_think",
        "CHAINLIT_THINKING_MODEL": "qwen38_think",
        "CHAINLIT_ENABLE_THINKING": "0",
        "VLLM_FORCE_DISABLE_THINKING": "1",
        "VLLM_LB_ENABLED": "1",
        "VLLM_LB_PORT": "12230",
        "VLLM_API_KEY": "sk-test-dual",
        "VLLM_PRIMARY_REQUEST_TIMEOUT_SECONDS": "60",
    }
    settings = build_litellm_failover_settings(env)
    rendered = render_litellm_config(template, settings, env=env)
    assert '"http://127.0.0.1:12230/v1"' in rendered
    assert '"sk-test-dual"' in rendered
    assert "enable_thinking: false" in rendered
    assert "qwen38_think" not in rendered
    assert "router_settings:" not in rendered


def test_litellm_config_direct_multi_replica_without_lb():
    template = Path(ROOT / "infra/litellm/config.yaml").read_text(encoding="utf-8")
    env = {
        "SERVED_MODEL_NAME": "qwen38",
        "CHAINLIT_DEFAULT_MODEL": "qwen38_no_think",
        "CHAINLIT_ENABLE_THINKING": "0",
        "VLLM_HOST": "127.0.0.1",
        "VLLM_PORT": "12233",
        "TP_SIZE": "2",
        "VLLM_REPLICA_GPU_GROUPS": "0,1;2,3",
        "VLLM_REMOTE_ORIGINS": "http://202.112.51.208:18000,http://202.112.51.208:18001",
    }
    settings = build_litellm_failover_settings(env)
    rendered = render_litellm_config(template, settings, env=env)
    assert '"http://127.0.0.1:12233/v1"' in rendered
    assert '"http://127.0.0.1:12234/v1"' in rendered
    assert '"http://202.112.51.208:18000/v1"' in rendered
    assert '"http://202.112.51.208:18001/v1"' in rendered
    assert "routing_strategy: least-busy" in rendered


def test_start_remote_vllm_command_includes_dual_host_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--model-path", "/models/qwen38",
            "--served-model-name", "qwen38",
            "--tensor-parallel-size", "2",
            "--language-model-only",
            "--disable-custom-all-reduce",
            "--reasoning-parser", "qwen3",
            "--api-key", "sk-secret",
        ]
    )
    cmd = build_command(config_from_args(args))
    assert "--language-model-only" in cmd
    assert "--disable-custom-all-reduce" in cmd
    assert "--reasoning-parser" in cmd and "qwen3" in cmd
    assert "--api-key" in cmd and "sk-secret" in cmd


def test_chainlit_error_formatting_and_reasoning_extractor():
    # Import helpers without booting full chainlit runtime by reading AST/module helpers
    chainlit_dir = str(ROOT / "apps/chainlit")
    if chainlit_dir not in sys.path:
        sys.path.insert(0, chainlit_dir)
    src = Path(ROOT / "apps/chainlit/app.py").read_text(encoding="utf-8")
    ns = {"Any": object, "Mapping": dict}
    from collections.abc import Mapping as AbcMapping
    ns["Mapping"] = AbcMapping
    start = src.index("def format_user_facing_upstream_error(")
    end = src.index("@cl.on_message")
    exec(src[start:end], ns)

    fmt = ns["format_user_facing_upstream_error"]
    extract = ns["extract_reasoning_token"]

    class APITimeoutError(Exception):
        pass

    msg = fmt(APITimeoutError("Read timed out"), quota_charged=False)
    assert "超时" in msg and "未消耗配额" in msg
    assert "Read timed out" not in msg

    msg_charged = fmt(APITimeoutError("Read timed out"), quota_charged=True)
    assert "超时" in msg_charged and "未消耗配额" not in msg_charged

    assert extract(SimpleNamespace(reasoning_content="step 1")) == "step 1"
    assert extract(SimpleNamespace(reasoning="step 2")) == "step 2"
    assert extract(SimpleNamespace(model_extra={"reasoning_content": "step 3"})) == "step 3"
    assert extract(SimpleNamespace(content="hello")) == ""
