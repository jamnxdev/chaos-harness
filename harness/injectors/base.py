"""Common interface every fault injector implements, so the scheduler/trial-runner
(Day 4-5) can drive process-kill, network-impairment, and resource-pressure injectors
identically without knowing their mechanism."""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class InjectionResult:
    fault_type: str
    target_component: str
    injected_at: float  # time.monotonic() timestamp


class FaultInjector(ABC):
    fault_type: str = "unspecified"

    @abstractmethod
    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        """Applies the fault for real (no simulation) and returns immediately once
        applied; recovery is a separate concern the trial runner tracks via the
        health probe, not something the injector itself waits for."""
        raise NotImplementedError

    def now(self) -> float:
        return time.monotonic()
