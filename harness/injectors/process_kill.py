"""Real process-kill fault: sends an actual SIGKILL to the target container's main
process via `docker kill`. No sleep()/simulated failure -- the whole point of this
project is that faults are real (spec's own design-decision section is explicit
about this).
"""
import subprocess

from harness.injectors.base import FaultInjector, InjectionResult
from harness.launcher import TargetSystem


class ProcessKillInjector(FaultInjector):
    fault_type = "process-kill"

    def __init__(self, target_system: TargetSystem):
        self.target_system = target_system

    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        injected_at = self.now()
        subprocess.run(["docker", "kill", "--signal=SIGKILL", target_component],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return InjectionResult(fault_type=self.fault_type, target_component=target_component, injected_at=injected_at)
