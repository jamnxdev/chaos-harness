"""Starts and stops the target system (3 API replicas, 1 KV store, 1 load balancer)
as plain OS subprocesses.

Deviation from the spec, flagged explicitly: the spec describes the target system
running "as containers" (docker-compose). This environment has no Docker installed
and no passwordless sudo to install it, so the target system runs as plain Python
subprocesses instead. This does not weaken the project's core claim (real fault
injection, not simulated) — process-kill still sends a real SIGKILL to a real OS
process, and Day 4's resource pressure uses real cgroup v2 limits. What is lost is
purely the container-runtime layer itself, which the spec already documents as a
"replaceable component," not part of the stable core.
"""
import subprocess
import sys
import time
from pathlib import Path

TARGET_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "target-system"

DEFAULT_REPLICA_PORTS = (9001, 9002, 9003)
DEFAULT_KV_PORT = 9010
DEFAULT_LB_PORT = 9000


class TargetSystem:
    """Owns the lifecycle of every target-system process. Component names used as
    keys throughout the harness: 'replica-1', 'replica-2', 'replica-3', 'kv-store',
    'load-balancer'."""

    def __init__(self, replica_ports=DEFAULT_REPLICA_PORTS, kv_port=DEFAULT_KV_PORT, lb_port=DEFAULT_LB_PORT):
        self.replica_ports = list(replica_ports)
        self.kv_port = kv_port
        self.lb_port = lb_port
        self.processes: dict[str, subprocess.Popen] = {}

    def start(self):
        for i, port in enumerate(self.replica_ports, start=1):
            name = f"replica-{i}"
            self.processes[name] = subprocess.Popen(
                [sys.executable, str(TARGET_SYSTEM_DIR / "api_replica.py"), "--port", str(port), "--id", str(i)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        self.processes["kv-store"] = subprocess.Popen(
            [sys.executable, str(TARGET_SYSTEM_DIR / "kv_store.py"), "--port", str(self.kv_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self.processes["load-balancer"] = subprocess.Popen(
            [sys.executable, str(TARGET_SYSTEM_DIR / "load_balancer.py"),
             "--port", str(self.lb_port), "--backend-ports", *map(str, self.replica_ports)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self

    def component_url(self, name: str) -> str:
        if name == "load-balancer":
            return f"http://127.0.0.1:{self.lb_port}"
        if name == "kv-store":
            return f"http://127.0.0.1:{self.kv_port}"
        idx = int(name.split("-")[1]) - 1
        return f"http://127.0.0.1:{self.replica_ports[idx]}"

    def pid(self, name: str) -> int:
        return self.processes[name].pid

    def stop(self, timeout=2.0):
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + timeout
        for proc in self.processes.values():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.processes.clear()
