"""Real CPU/memory pressure fault: runs actual `stress-ng` inside the target
container via `docker exec -d`, contending against that container's own real cgroup
cpu/memory limits (`cpus`/`mem_limit` in docker-compose.yml) -- not a sleep()-based
simulation, and not pressure applied to the whole host.

Unlike the process-kill and network-impairment injectors, this one needs no explicit
heal() step: `stress-ng --timeout <duration_s>s` is told to stop itself, and `docker
exec -d` runs it detached so `inject()` returns immediately per the FaultInjector
contract, without the harness needing to track or cancel it.
"""
import subprocess

from harness.injectors.base import FaultInjector, InjectionResult
from harness.launcher import TargetSystem


class ResourcePressureInjector(FaultInjector):
    def __init__(self, target_system: TargetSystem, kind: str = "cpu",
                 duration_s: float = 2.0, cpu_workers: int = 2, vm_bytes: str = "100M"):
        if kind not in ("cpu", "memory"):
            raise ValueError(f"unknown kind: {kind!r}")
        self.target_system = target_system
        self.kind = kind
        self.duration_s = duration_s
        self.cpu_workers = cpu_workers
        self.vm_bytes = vm_bytes
        self.fault_type = "cpu-pressure" if kind == "cpu" else "memory-pressure"

    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        duration_s = kwargs.get("duration_s", self.duration_s)
        timeout_arg = f"{duration_s}s"

        if self.kind == "cpu":
            stress_args = ["--cpu", str(self.cpu_workers), "--cpu-method", "all"]
        else:
            # --vm-keep holds the allocated pages resident instead of freeing and
            # re-allocating every iteration, so this measures sustained memory
            # pressure against the container's mem_limit, not allocation churn.
            stress_args = ["--vm", "1", "--vm-bytes", self.vm_bytes, "--vm-keep"]

        injected_at = self.now()
        subprocess.run(
            ["docker", "exec", "-d", target_component, "stress-ng", *stress_args, "--timeout", timeout_arg],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return InjectionResult(fault_type=self.fault_type, target_component=target_component, injected_at=injected_at)

    def is_running(self, target_component: str) -> bool:
        """Checked via `docker top` (host-side process listing for the container)
        rather than `docker exec ... pgrep`, since the target image is a minimal
        Python base without `procps` installed."""
        result = subprocess.run(
            ["docker", "top", target_component],
            capture_output=True, text=True,
        )
        return "stress-ng" in result.stdout
