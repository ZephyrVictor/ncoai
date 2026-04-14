from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

import devctl
from QPS_test.common import DEVCTL_LOG_DIR, LogCapStatus, RunConfig, directory_size_bytes


OVERRIDE_HEADER = "# QPS_test managed overrides"


def strip_managed_overrides(text: str) -> str:
    marker = f"\n{OVERRIDE_HEADER}"
    if text.startswith(f"{OVERRIDE_HEADER}\n"):
        return ""
    if marker in text:
        return text.split(marker, 1)[0].rstrip() + "\n"
    return text


def render_env_assignments(values: dict[str, str]) -> str:
    lines = [OVERRIDE_HEADER]
    for key, value in values.items():
        rendered = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{rendered}"')
    return "\n".join(lines) + "\n"


@dataclass(slots=True)
class HostEnvOverrideManager:
    path: Path
    original_text: str | None = None
    applied_overrides: dict[str, str] | None = None

    def apply(self, overrides: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.original_text is None:
            current_text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            self.original_text = strip_managed_overrides(current_text)

        combined = self.original_text.rstrip()
        if combined:
            combined += "\n\n"
        combined += render_env_assignments(overrides)
        self.path.write_text(combined, encoding="utf-8")
        self.applied_overrides = dict(overrides)

    def restore(self) -> None:
        if self.original_text is None:
            return
        self.path.write_text(self.original_text, encoding="utf-8")
        self.applied_overrides = None


def run_devctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(devctl.ROOT / "devctl.py"), *args],
        cwd=devctl.ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def service_probe_urls(service: str, env: dict[str, str]) -> list[str]:
    if service == "litellm":
        port = env.get("LITELLM_PORT", "33455")
        return [f"http://127.0.0.1:{port}/v1/models"]
    return devctl.service_readiness_urls(service, env)


def assert_devctl_ok(args: list[str]) -> None:
    result = run_devctl(args)
    if result.returncode != 0:
        raise RuntimeError(
            f"devctl {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}".strip()
        )


async def wait_for_service_urls(service_names: Iterable[str]) -> None:
    env = devctl.load_env("host")
    required_success_streak = 2
    async with httpx.AsyncClient(timeout=5.0) as client:
        for service in service_names:
            urls = devctl.service_readiness_urls(service, env)
            if not urls:
                continue
            deadline = asyncio.get_running_loop().time() + devctl.SERVICES[
                service
            ].readiness_timeout_seconds
            last_error = "service did not become ready"
            success_streak = 0
            while asyncio.get_running_loop().time() < deadline:
                cycle_healthy = False
                for url in urls:
                    try:
                        response = await client.get(url)
                        if 200 <= response.status_code < 500:
                            cycle_healthy = True
                            break
                        last_error = f"unexpected status {response.status_code}"
                    except Exception as exc:  # pragma: no cover - network path
                        last_error = str(exc)
                if cycle_healthy:
                    success_streak += 1
                    last_error = ""
                else:
                    success_streak = 0
                if success_streak >= required_success_streak:
                    break
                await asyncio.sleep(1.0)
            if last_error:
                raise RuntimeError(f"{service} readiness check failed: {last_error}")


async def ensure_services_ready(service_names: Iterable[str]) -> None:
    names = list(service_names)
    status_result = run_devctl(["status", *names])
    if status_result.returncode != 0:
        raise RuntimeError(
            f"devctl status failed:\n{status_result.stdout}\n{status_result.stderr}".strip()
        )
    await wait_for_service_urls(names)


async def restart_services(service_names: Iterable[str]) -> None:
    names = list(service_names)
    assert_devctl_ok(["down", *names])
    assert_devctl_ok(["up", *names])
    await wait_for_service_urls(names)


@dataclass(slots=True)
class LogCapMonitor:
    max_bytes: int
    warning_ratio: float
    warning_triggered: bool = False
    abort_triggered: bool = False

    def check(self) -> LogCapStatus:
        size_bytes = directory_size_bytes(DEVCTL_LOG_DIR)
        if size_bytes >= int(self.max_bytes * self.warning_ratio):
            self.warning_triggered = True
        if size_bytes >= self.max_bytes:
            self.abort_triggered = True
        return LogCapStatus(
            size_bytes=size_bytes,
            warning_triggered=self.warning_triggered,
            abort_triggered=self.abort_triggered,
        )


def parse_percent_value(value: str) -> float | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        return float(raw) / 100.0
    except ValueError:
        return None


def host_memory_usage_ratio() -> float | None:
    try:
        total_kb = None
        available_kb = None
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available_kb = int(line.split()[1])
        if not total_kb or available_kb is None:
            return None
        used_kb = max(total_kb - available_kb, 0)
        return used_kb / total_kb
    except (OSError, ValueError, IndexError):
        return None


def docker_container_memory_ratios(container_names: list[str]) -> dict[str, float | None]:
    if not container_names:
        return {}
    try:
        result = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}|{{.MemPerc}}",
                *container_names,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}

    ratios: dict[str, float | None] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        name, mem_perc = line.split("|", 1)
        ratios[name] = parse_percent_value(mem_perc)
    return ratios


def gpu_memory_usage_ratio() -> tuple[float | None, bool]:
    if not shutil.which("nvidia-smi"):
        return None, True
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, True
    if result.returncode != 0:
        return None, True

    ratios: list[float] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            used_raw, total_raw = [part.strip() for part in line.split(",", 1)]
            used = float(used_raw)
            total = float(total_raw)
        except (ValueError, IndexError):
            continue
        if total > 0:
            ratios.append(used / total)
    if not ratios:
        return None, True
    return max(ratios), False


@dataclass(slots=True)
class SafetySnapshot:
    service_health: dict[str, tuple[bool, str | None]]
    host_memory_ratio: float | None
    postgres_memory_ratio: float | None
    redis_memory_ratio: float | None
    gpu_memory_ratio: float | None
    gpu_probe_failed: bool


async def check_service_health(service_names: Iterable[str]) -> dict[str, tuple[bool, str | None]]:
    env = devctl.load_env("host")
    names = list(service_names)
    async with httpx.AsyncClient(timeout=5.0) as client:
        async def probe_one(service: str) -> tuple[str, tuple[bool, str | None]]:
            urls = service_probe_urls(service, env)
            if not urls:
                return service, (True, None)
            healthy = False
            last_error: str | None = "unknown error"
            for url in urls:
                try:
                    response = await client.get(url)
                    if 200 <= response.status_code < 500:
                        healthy = True
                        last_error = None
                        break
                    last_error = f"unexpected status {response.status_code}"
                except Exception as exc:  # pragma: no cover - network path
                    last_error = str(exc)
            return service, (healthy, last_error)

        pairs = await asyncio.gather(*(probe_one(service) for service in names))
    return dict(pairs)


class ResourceSafetyMonitor:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self._service_failures = {name: 0 for name in ("chainlit", "litellm", "vllm")}
        self._resource_streaks = {
            "host_memory": 0,
            "postgres_memory": 0,
            "redis_memory": 0,
            "gpu_memory": 0,
        }

    async def sample(self) -> SafetySnapshot:
        service_health = await check_service_health(self._service_failures.keys())
        container_ratios = await asyncio.to_thread(
            docker_container_memory_ratios,
            [self.config.postgres_container_name, self.config.redis_container_name],
        )
        host_ratio = await asyncio.to_thread(host_memory_usage_ratio)
        gpu_ratio, gpu_probe_failed = await asyncio.to_thread(gpu_memory_usage_ratio)
        return SafetySnapshot(
            service_health=service_health,
            host_memory_ratio=host_ratio,
            postgres_memory_ratio=container_ratios.get(self.config.postgres_container_name),
            redis_memory_ratio=container_ratios.get(self.config.redis_container_name),
            gpu_memory_ratio=gpu_ratio,
            gpu_probe_failed=gpu_probe_failed,
        )

    def evaluate(self, snapshot: SafetySnapshot) -> str | None:
        for service, (healthy, error) in snapshot.service_health.items():
            if healthy:
                self._service_failures[service] = 0
                continue
            self._service_failures[service] += 1
            if self._service_failures[service] >= self.config.service_health_abort_failures:
                detail = error or "service health check failed"
                return (
                    f"{service} health failed {self._service_failures[service]} times: {detail}"
                )

        host_result = self._update_resource_streak(
            "host_memory",
            snapshot.host_memory_ratio,
            self.config.host_memory_abort_ratio,
            "host memory usage remained above threshold",
        )
        if host_result:
            return host_result

        postgres_result = self._update_resource_streak(
            "postgres_memory",
            snapshot.postgres_memory_ratio,
            self.config.postgres_memory_abort_ratio,
            "postgres container memory usage remained above threshold",
        )
        if postgres_result:
            return postgres_result

        redis_result = self._update_resource_streak(
            "redis_memory",
            snapshot.redis_memory_ratio,
            self.config.redis_memory_abort_ratio,
            "redis container memory usage remained above threshold",
        )
        if redis_result:
            return redis_result

        gpu_trigger_value = snapshot.gpu_memory_ratio
        if snapshot.gpu_probe_failed and not snapshot.service_health.get("vllm", (True, None))[0]:
            gpu_trigger_value = 1.0
        gpu_result = self._update_resource_streak(
            "gpu_memory",
            gpu_trigger_value,
            self.config.gpu_memory_abort_ratio,
            "GPU memory usage remained above threshold",
        )
        if gpu_result:
            return gpu_result
        return None

    def _update_resource_streak(
        self,
        key: str,
        ratio: float | None,
        threshold: float,
        message: str,
    ) -> str | None:
        if ratio is not None and ratio >= threshold:
            self._resource_streaks[key] += 1
            if self._resource_streaks[key] >= self.config.resource_abort_streak:
                return (
                    f"{message}: {ratio:.2%} for "
                    f"{self._resource_streaks[key]} consecutive samples"
                )
            return None
        self._resource_streaks[key] = 0
        return None


def build_runtime_overrides(config: RunConfig) -> dict[str, str]:
    overrides = {
        "CHAT_AUTH_MODE": "managed_user",
        "LITELLM_SET_VERBOSE": "0",
        "CHAINLIT_QUOTA_DEFAULT_MAX_COMPLETION_TOKENS": str(
            config.runtime_completion_tokens
        ),
    }
    if config.user_limit_tokens_override is not None:
        overrides["CHAINLIT_USER_LIMIT_TOKENS"] = str(config.user_limit_tokens_override)
    if config.thread_limit_tokens_override is not None:
        overrides["CHAINLIT_THREAD_LIMIT_TOKENS"] = str(
            config.thread_limit_tokens_override
        )
    return overrides


def dump_runtime_state(
    config: RunConfig,
    manager: HostEnvOverrideManager,
) -> dict[str, object]:
    return {
        "host_env_path": str(manager.path),
        "overrides": manager.applied_overrides or {},
    }
