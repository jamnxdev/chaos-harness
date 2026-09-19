"""Starts and stops the target system (3 API replicas, 1 KV store, 1 load balancer),
running as real Docker containers via `docker compose`, per the spec's architecture.

Each component is wrapped in a `ContainerSupervisor` (see container_supervisor.py) so a
process-kill fault has something driving recovery -- Docker's own `restart:
unless-stopped` policy is declared in docker-compose.yml too, but was found, by direct
testing, not to reliably fire in this environment, so the harness does not depend on it.
"""
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from harness.container_supervisor import ContainerSupervisor

TARGET_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "target-system"

DEFAULT_HOST_PORTS = {
    "replica-1": 9001,
    "replica-2": 9002,
    "replica-3": 9003,
    "kv-store": 9010,
    "load-balancer": 9000,
}

_PORT_ENV_VARS = {
    "replica-1": "REPLICA1_PORT",
    "replica-2": "REPLICA2_PORT",
    "replica-3": "REPLICA3_PORT",
    "kv-store": "KV_PORT",
    "load-balancer": "LB_PORT",
}


class TargetSystem:
    """Component names used as keys throughout the harness: 'replica-1', 'replica-2',
    'replica-3', 'kv-store', 'load-balancer' -- these are also the containers' names
    (docker-compose.yml sets `container_name` to match, so `docker kill <name>` and
    this class's naming are always the same string)."""

    def __init__(self, host_ports: dict | None = None, restart_delay: float = 0.05):
        self.host_ports = {**DEFAULT_HOST_PORTS, **(host_ports or {})}
        self.restart_delay = restart_delay
        self.supervisors: dict[str, ContainerSupervisor] = {}

    def start(self, build: bool = True, wait_ready: float = 10.0):
        env = {**os.environ, **{_PORT_ENV_VARS[name]: str(port) for name, port in self.host_ports.items()}}
        args = ["docker", "compose", "up", "-d"]
        if build:
            args.append("--build")
        subprocess.run(args, cwd=TARGET_SYSTEM_DIR, env=env, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        for name in DEFAULT_HOST_PORTS:
            self.supervisors[name] = ContainerSupervisor(name, self.restart_delay).start()

        self._wait_all_healthy(timeout=wait_ready)
        return self

    def _wait_all_healthy(self, timeout: float):
        """Waits for every component's own /health endpoint to answer 200, not just
        for Docker to report the container as 'Running' -- a container can be
        'Running' before its process has finished binding its listening socket, and
        the load balancer depends on all three replicas actually being reachable.
        Caught by test flakiness before this existed: the LB's very first proxied
        requests occasionally hit a replica whose socket wasn't bound yet, which
        urllib surfaces as a raw `ConnectionResetError` rather than a clean refusal."""
        deadline = time.monotonic() + timeout
        for name in self.supervisors:
            url = f"{self.component_url(name)}/health"
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url, timeout=0.3) as resp:
                        if resp.status == 200:
                            break
                except (urllib.error.URLError, ConnectionError, OSError):
                    pass
                time.sleep(0.05)

    def component_url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.host_ports[name]}"

    def pid(self, name: str) -> int:
        return self.supervisors[name].pid

    def supervisor(self, name: str) -> ContainerSupervisor:
        return self.supervisors[name]

    def stop(self, timeout: float = 5.0):
        for sup in self.supervisors.values():
            sup.stop(timeout=timeout)
        self.supervisors.clear()
        subprocess.run(["docker", "compose", "down"], cwd=TARGET_SYSTEM_DIR,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
