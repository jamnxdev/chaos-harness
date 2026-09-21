"""The Day 5 full experiment batch: >=20 trials per fault type, across every
injector built so far, against the real Docker-based target system, persisted to
`results/results.db`. This is what `run-experiments.sh` invokes.

Target-component choices are deliberate, not arbitrary: process-kill runs against
both a replica (masked by the load balancer) and the singleton kv-store (a genuine
full outage), to keep that blast-radius asymmetry visible in the raw data even
though nothing here computes an LB-availability metric yet (still a known Day 2 gap,
carried forward to the eventual Day 6 report). Network and resource faults each get
their own dedicated replica so no two fault types share a target's recent history.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.injectors.process_kill import ProcessKillInjector
from harness.injectors.network_impairment import NetworkImpairmentInjector
from harness.injectors.resource_pressure import ResourcePressureInjector
from harness.recorder import ResultRecorder
from harness.scheduler import ExperimentScheduler

RESULTS_DB = Path(__file__).resolve().parent.parent / "results" / "results.db"
N_TRIALS = 20


def main():
    system = TargetSystem(restart_delay=0.05).start()
    recorder = ResultRecorder(RESULTS_DB)
    try:
        process_kill = ProcessKillInjector(system)
        network_partition = NetworkImpairmentInjector(system, mode="partition", duration_s=1.0)
        network_latency = NetworkImpairmentInjector(system, mode="latency", delay_ms=250, duration_s=1.0)
        cpu_pressure = ResourcePressureInjector(system, kind="cpu", duration_s=1.5, cpu_workers=2)
        memory_pressure = ResourcePressureInjector(system, kind="memory", duration_s=1.5, vm_bytes="100M")

        scheduler = ExperimentScheduler(
            system, recorder, run_id="day5-full-batch",
            detection_timeout=1.5, recovery_timeout=5.0, stabilization_pause=0.3,
        )
        scheduler.add(process_kill, "replica-1", n_trials=N_TRIALS)
        scheduler.add(process_kill, "kv-store", n_trials=N_TRIALS)
        scheduler.add(network_partition, "replica-2", n_trials=N_TRIALS)
        scheduler.add(network_latency, "replica-2", n_trials=N_TRIALS)
        scheduler.add(cpu_pressure, "replica-3", n_trials=N_TRIALS)
        scheduler.add(memory_pressure, "replica-3", n_trials=N_TRIALS)

        results = scheduler.run_all()

        print(f"{'fault_type':<20} {'component':<12} {'n':>3} {'detected':>9} {'recovered':>10}")
        for (fault_type, component), records in results.items():
            detected = sum(r.detected for r in records)
            recovered = sum(r.recovered for r in records)
            print(f"{fault_type:<20} {component:<12} {len(records):>3} {detected:>9} {recovered:>10}")
    finally:
        recorder.close()
        system.stop()


if __name__ == "__main__":
    main()
