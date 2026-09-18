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
from pathlib import Path

from harness.supervisor import ComponentSupervisor

TARGET_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "target-system"

DEFAULT_REPLICA_PORTS = (9001, 9002, 9003)
DEFAULT_KV_PORT = 9010
DEFAULT_LB_PORT = 9000


def _spawn(args):
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TargetSystem:
    """Owns the lifecycle of every target-system process. Component names used as
    keys throughout the harness: 'replica-1', 'replica-2', 'replica-3', 'kv-store',
    'load-balancer'.

    Every component is wrapped in a `ComponentSupervisor` (not a bare `Popen`), since
    the process-kill fault injector (Day 2) needs *something* to restart a killed
    process for there to be any recovery to measure -- see supervisor.py."""

    def __init__(self, replica_ports=DEFAULT_REPLICA_PORTS, kv_port=DEFAULT_KV_PORT, lb_port=DEFAULT_LB_PORT,
                 restart_delay=0.05):
        self.replica_ports = list(replica_ports)
        self.kv_port = kv_port
        self.lb_port = lb_port
        self.restart_delay = restart_delay
        self.supervisors: dict[str, ComponentSupervisor] = {}

    def start(self):
        for i, port in enumerate(self.replica_ports, start=1):
            name = f"replica-{i}"
            args = [sys.executable, str(TARGET_SYSTEM_DIR / "api_replica.py"),
                    "--port", str(port), "--id", str(i)]
            self.supervisors[name] = ComponentSupervisor(name, lambda a=args: _spawn(a), self.restart_delay).start()

        kv_args = [sys.executable, str(TARGET_SYSTEM_DIR / "kv_store.py"), "--port", str(self.kv_port)]
        self.supervisors["kv-store"] = ComponentSupervisor(
            "kv-store", lambda a=kv_args: _spawn(a), self.restart_delay).start()

        lb_args = [sys.executable, str(TARGET_SYSTEM_DIR / "load_balancer.py"),
                   "--port", str(self.lb_port), "--backend-ports", *map(str, self.replica_ports)]
        self.supervisors["load-balancer"] = ComponentSupervisor(
            "load-balancer", lambda a=lb_args: _spawn(a), self.restart_delay).start()
        return self

    def component_url(self, name: str) -> str:
        if name == "load-balancer":
            return f"http://127.0.0.1:{self.lb_port}"
        if name == "kv-store":
            return f"http://127.0.0.1:{self.kv_port}"
        idx = int(name.split("-")[1]) - 1
        return f"http://127.0.0.1:{self.replica_ports[idx]}"

    def pid(self, name: str) -> int:
        return self.supervisors[name].pid

    def supervisor(self, name: str) -> ComponentSupervisor:
        return self.supervisors[name]

    def stop(self, timeout=2.0):
        for sup in self.supervisors.values():
            sup.stop(timeout=timeout)
        self.supervisors.clear()
