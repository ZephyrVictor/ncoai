#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ncoai_shared.vllm_failover import prepare_litellm_config


DEFAULT_TEMPLATE_PATH = ROOT_DIR / "infra" / "litellm" / "config.yaml"
DEFAULT_OUTPUT_PATH = ROOT_DIR / ".local" / "generated" / "litellm.config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the LiteLLM runtime config")
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE_PATH),
        help="Path to the tracked LiteLLM config template.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to the rendered runtime config file.",
    )
    parser.add_argument(
        "--probe-timeout-seconds",
        type=float,
        default=float(os.getenv("ADMIN_VLLM_REDUNDANCY_PROBE_TIMEOUT_SECONDS", "3")),
        help="Timeout for probing the backup vLLM endpoint.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template_path = Path(args.template)
    output_path = Path(args.output)

    template_text = template_path.read_text(encoding="utf-8")
    prepared = prepare_litellm_config(
        template_text,
        env=os.environ,
        probe_timeout_seconds=args.probe_timeout_seconds,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prepared.rendered_config, encoding="utf-8")

    print(f"[INFO] Rendered LiteLLM config: {output_path}")
    if prepared.settings.lastdance_enabled:
        print(
            "[INFO] Lastdance mode enabled, all traffic routed to BigModel "
            f"via {prepared.settings.lastdance_endpoint.api_base}"
        )
        print(
            f"[INFO] Lastdance target model: {prepared.settings.lastdance_target_model}"
        )
    else:
        print(
            f"[INFO] Primary router api_base: {prepared.settings.lastdance_endpoint.api_base}"
        )
        print(
            "[INFO] Local vLLM origin behind router: "
            f"{prepared.settings.primary_endpoint.origin}"
        )
    for warning in prepared.warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
