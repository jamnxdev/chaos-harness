"""Ties injector + health probe + recorder together into the spec's own data-flow
description: scheduler/caller triggers an experiment -> injector applies the fault ->
probe polls system health -> on recovery (or timeout) the timeline is recorded ->
result appended to the run database.
"""
import time

from harness.probe import HealthProbe
from harness.recorder import ResultRecorder, TrialRecord


class TrialRunner:
    def __init__(self, target_system, injector, recorder: ResultRecorder, run_id: str,
                 detection_timeout: float = 3.0, recovery_timeout: float = 5.0,
                 stabilization_pause: float = 0.5, poll_interval: float = 0.02):
        self.target_system = target_system
        self.injector = injector
        self.recorder = recorder
        self.run_id = run_id
        self.detection_timeout = detection_timeout
        self.recovery_timeout = recovery_timeout
        self.stabilization_pause = stabilization_pause
        self.poll_interval = poll_interval

    def run_trial(self, target_component: str, **inject_kwargs) -> TrialRecord:
        probe = HealthProbe(
            self.target_system.component_url(target_component), poll_interval=self.poll_interval
        ).start()
        try:
            # Every trial starts from a known-good baseline -- a trial run against an
            # already-unhealthy component would produce a meaningless (or negative)
            # detection latency.
            healthy_at_start = probe.wait_for(True, timeout=self.detection_timeout)
            if healthy_at_start is None:
                raise RuntimeError(
                    f"{target_component} was not healthy before fault injection; "
                    "refusing to run a trial against a component in an unknown state"
                )

            result = self.injector.inject(target_component, **inject_kwargs)

            first_unhealthy_at = probe.wait_for(False, timeout=self.detection_timeout)
            detected = first_unhealthy_at is not None
            detection_latency_ms = (
                (first_unhealthy_at - result.injected_at) * 1000.0 if detected else None
            )

            recovered = False
            recovery_time_ms = None
            if detected:
                recovered_at = probe.wait_for(True, timeout=self.recovery_timeout)
                recovered = recovered_at is not None
                if recovered:
                    recovery_time_ms = (recovered_at - result.injected_at) * 1000.0

            record = TrialRecord(
                run_id=self.run_id,
                fault_type=self.injector.fault_type,
                target_component=target_component,
                detected=detected,
                recovered=recovered,
                detection_latency_ms=detection_latency_ms,
                recovery_time_ms=recovery_time_ms,
                notes="" if detected else "no unhealthy transition observed within detection_timeout",
            )
            self.recorder.record_trial(record)
            return record
        finally:
            probe.stop()
            # Stabilization pause between trials, per the spec's experimental-design
            # section, so one trial's aftermath (a still-restarting process) can't
            # contaminate the next trial's baseline-healthy check.
            time.sleep(self.stabilization_pause)

    def run_batch(self, target_component: str, n_trials: int, **inject_kwargs) -> list[TrialRecord]:
        return [self.run_trial(target_component, **inject_kwargs) for _ in range(n_trials)]
