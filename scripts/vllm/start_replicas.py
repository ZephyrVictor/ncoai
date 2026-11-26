#!/usr/bin/env python3
"""Supervise local vLLM replicas and release their process groups on exit."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ncoai_shared.vllm_replicas import replica_environments


def stop_signal(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    replicas = replica_environments(os.environ)
    # Check every port before launching anything; never take over another server.
    for env in replicas:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((env.get("VLLM_HOST", "127.0.0.1"), int(env["VLLM_PORT"])))
    children = []
    try:
        for index, env in enumerate(replicas):
            print(f"[INFO] replica {index}: GPUs={env['CUDA_VISIBLE_DEVICES']} port={env['VLLM_PORT']}", flush=True)
            children.append(subprocess.Popen(
                [sys.executable, str(ROOT / "scripts/vllm/start_remote_vllm.py")],
                env=env, cwd=ROOT, start_new_session=True,
            ))
        while True:
            for index, child in enumerate(children):
                if child.poll() is not None:
                    print(f"[ERROR] vLLM replica {index} exited: {child.returncode}", flush=True)
                    return child.returncode or 1
            time.sleep(.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # devctl allows 20 seconds before escalating the supervisor itself.
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(deadline - time.monotonic(), .1))
            except subprocess.TimeoutExpired:
                pass
            # Workers may outlive the API process. Only signal groups we own.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGHUP, stop_signal)
    raise SystemExit(main())
