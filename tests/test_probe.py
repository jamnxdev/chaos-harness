"""Tests HealthProbe against the real (Docker-based) target system, not a mock server
-- the probe's entire job is polling a real socket, so a mocked HTTP layer would test
nothing meaningful about it."""
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.probe import HealthProbe


def real_kill(container_name: str):
    subprocess.run(["docker", "kill", "--signal=SIGKILL", container_name],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class HealthProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem().start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def test_probe_reports_healthy_once_target_is_up(self):
        probe = HealthProbe(self.system.component_url("replica-1"), poll_interval=0.02).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=3.0))
        finally:
            probe.stop()

    def test_probe_detects_transition_to_unhealthy_after_container_is_killed(self):
        probe = HealthProbe(self.system.component_url("replica-2"), poll_interval=0.02).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=5.0))
            real_kill("replica-2")
            observed_at = probe.wait_for(False, timeout=3.0)
            self.assertIsNotNone(observed_at, "probe never observed the killed replica as unhealthy")
        finally:
            probe.stop()
            # give the supervisor a moment to bring it back before the next test
            HealthProbe(self.system.component_url("replica-2")).start().wait_for(True, timeout=5.0)

    def test_transition_callback_fires_with_correct_boolean(self):
        transitions = []
        probe = HealthProbe(
            self.system.component_url("replica-3"),
            on_transition=lambda healthy, ts: transitions.append(healthy),
            poll_interval=0.02,
        ).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=5.0))
            real_kill("replica-3")
            self.assertIsNotNone(probe.wait_for(False, timeout=3.0))
            self.assertIn(False, transitions)
        finally:
            probe.stop()
            HealthProbe(self.system.component_url("replica-3")).start().wait_for(True, timeout=5.0)

    def test_wait_for_times_out_when_condition_never_met(self):
        probe = HealthProbe("http://127.0.0.1:19199", poll_interval=0.02).start()  # nothing listening
        try:
            self.assertIsNone(probe.wait_for(True, timeout=0.2))
        finally:
            probe.stop()


if __name__ == "__main__":
    unittest.main()
