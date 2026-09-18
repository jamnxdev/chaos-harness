"""Real process-kill fault: sends an actual SIGKILL to the target component's current
OS PID, via the component's own ComponentSupervisor. No sleep()/simulated failure --
the whole point of this project is that faults are real (spec's own design-decision
section is explicit about this).
"""
import os
import signal

from harness.injectors.base import FaultInjector, InjectionResult
from harness.launcher import TargetSystem


class ProcessKillInjector(FaultInjector):
    fault_type = "process-kill"

    def __init__(self, target_system: TargetSystem):
        self.target_system = target_system

    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        supervisor = self.target_system.supervisor(target_component)
        pid = supervisor.pid
        injected_at = self.now()
        os.kill(pid, signal.SIGKILL)
        return InjectionResult(fault_type=self.fault_type, target_component=target_component, injected_at=injected_at)
