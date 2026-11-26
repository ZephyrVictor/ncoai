"""Resolve local and remote model replicas consistently for launch, readiness and routing."""
from __future__ import annotations

import re
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


def local_replica_origins(env: Mapping[str, str]) -> list[str]:
    """Return http://host:port origins for all local vLLM replicas."""
    origins: list[str] = []
    for replica in replica_environments(env):
        host = str(replica.get("VLLM_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = str(replica.get("VLLM_PORT") or "12233").strip() or "12233"
        origins.append(f"http://{host}:{port}")
    return origins


def remote_replica_origins(env: Mapping[str, str]) -> list[str]:
    """Parse comma/semicolon/whitespace-separated remote vLLM origins."""
    raw = str(env.get("VLLM_REMOTE_ORIGINS") or "").strip()
    if not raw:
        return []
    origins: list[str] = []
    for item in re.split(r"[,;\s]+", raw):
        token = item.strip().rstrip("/")
        if not token:
            continue
        if "://" not in token:
            token = f"http://{token}"
        if token.endswith("/v1"):
            token = token[:-3]
        if token not in origins:
            origins.append(token)
    return origins


def all_vllm_origins(env: Mapping[str, str]) -> list[str]:
    """Return deduplicated local + remote vLLM replica origins in deterministic order."""
    origins: list[str] = []
    for origin in (*local_replica_origins(env), *remote_replica_origins(env)):
        if origin not in origins:
            origins.append(origin)
    return origins
