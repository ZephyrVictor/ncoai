"""Resolve local model replicas consistently for launch, readiness and routing."""
from __future__ import annotations

from collections.abc import Mapping


def replica_environments(env: Mapping[str, str]) -> list[dict[str, str]]:
    """Optional semicolon-separated GPU groups use consecutive VLLM_PORTs."""
    groups = env.get("VLLM_REPLICA_GPU_GROUPS", "").strip()
    if not groups:
        return [dict(env)]
    port = int(env.get("VLLM_PORT", "12233"))
    tp = int(env.get("TP_SIZE", "4"))
    seen: set[str] = set()
    replicas = []
    for index, group in enumerate(groups.split(";")):
        devices = [device.strip() for device in group.split(",")]
        if tp < 1 or len(devices) != tp or any(not device for device in devices):
            raise ValueError("Each VLLM_REPLICA_GPU_GROUPS group must contain TP_SIZE devices")
        if len(set(devices)) != len(devices) or seen.intersection(devices):
            raise ValueError("VLLM replicas must use distinct GPU devices")
        if not 1 <= port + index <= 65535:
            raise ValueError("VLLM replica port is out of range")
        seen.update(devices)
        child = dict(env)
        child.update(CUDA_VISIBLE_DEVICES=",".join(devices), VLLM_PORT=str(port + index))
        # A child represents one server; do not recursively launch more replicas.
        child.pop("VLLM_REPLICA_GPU_GROUPS", None)
        replicas.append(child)
    return replicas
