"""Exercises real stress-ng against real, cgroup-limited (cpus/mem_limit) Docker
containers -- no simulated pressure. Requires the target image to have been rebuilt
with stress-ng installed (target-system/Dockerfile)."""
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.injectors.resource_pressure import ResourcePressureInjector
from harness.probe import HealthProbe


def cpu_percent(container_name: str) -> float:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container_name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out.rstrip("%"))


class ResourcePressureInjectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem(restart_delay=0.02).start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def test_cpu_pressure_is_real_and_self_terminates(self):
        injector = ResourcePressureInjector(self.system, kind="cpu", duration_s=2.0, cpu_workers=2)
        self.assertEqual(injector.fault_type, "cpu-pressure")

        baseline = cpu_percent("replica-1")
        injector.inject("replica-1")
        time.sleep(0.5)

        self.assertTrue(injector.is_running("replica-1"), "stress-ng did not appear in the container")
        under_load = cpu_percent("replica-1")
        self.assertGreater(under_load, baseline + 10, "CPU usage did not measurably increase under stress-ng")

        deadline = time.monotonic() + 4.0
        while injector.is_running("replica-1") and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(injector.is_running("replica-1"), "stress-ng outlived its --timeout")

    def test_health_probe_stays_healthy_under_moderate_cpu_pressure(self):
        # A direct, reproducible instance of the false-negative-rate concern flagged
        # in the log: the target is genuinely under real, measured CPU contention,
        # but the health check is cheap enough that it still passes throughout.
        injector = ResourcePressureInjector(self.system, kind="cpu", duration_s=1.5, cpu_workers=2)
        probe = HealthProbe(self.system.component_url("replica-2"), poll_interval=0.05).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=3.0))
            injector.inject("replica-2")
            time.sleep(1.0)  # well within the stress window
            self.assertTrue(probe.is_healthy, "health probe falsely reported unhealthy under moderate CPU pressure")
        finally:
            probe.stop()

    def test_memory_pressure_under_the_container_limit_does_not_oom(self):
        injector = ResourcePressureInjector(self.system, kind="memory", duration_s=2.0, vm_bytes="100M")
        self.assertEqual(injector.fault_type, "memory-pressure")
        injector.inject("replica-3")
        time.sleep(0.5)
        self.assertTrue(injector.is_running("replica-3"))

        deadline = time.monotonic() + 4.0
        while injector.is_running("replica-3") and time.monotonic() < deadline:
            time.sleep(0.2)

        status = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.OOMKilled}}", "replica-3"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(status, "running false")


if __name__ == "__main__":
    unittest.main()
