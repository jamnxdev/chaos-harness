"""Tests HealthProbe against the real target system, not a mock server — the probe's
entire job is polling a real socket, so a mocked HTTP layer would test nothing
meaningful about it."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.probe import HealthProbe

TEST_REPLICA_PORTS = (19101, 19102, 19103)
TEST_KV_PORT = 19110
TEST_LB_PORT = 19100


class HealthProbeTest(unittest.TestCase):
    def setUp(self):
        self.system = TargetSystem(TEST_REPLICA_PORTS, TEST_KV_PORT, TEST_LB_PORT).start()

    def tearDown(self):
        self.system.stop()

    def test_probe_reports_healthy_once_target_is_up(self):
        probe = HealthProbe(self.system.component_url("replica-1"), poll_interval=0.02).start()
        try:
            deadline = time.monotonic() + 3.0
            while probe.is_healthy is not True and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(probe.is_healthy)
        finally:
            probe.stop()

    def test_probe_detects_transition_to_unhealthy_after_process_is_killed(self):
        probe = HealthProbe(self.system.component_url("replica-2"), poll_interval=0.02).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=3.0))
            self.system.supervisor("replica-2").process.kill()
            self.system.supervisor("replica-2").process.wait()
            observed_at = probe.wait_for(False, timeout=3.0)
            self.assertIsNotNone(observed_at, "probe never observed the killed replica as unhealthy")
        finally:
            probe.stop()

    def test_transition_callback_fires_with_correct_boolean(self):
        transitions = []
        probe = HealthProbe(
            self.system.component_url("replica-3"),
            on_transition=lambda healthy, ts: transitions.append(healthy),
            poll_interval=0.02,
        ).start()
        try:
            self.assertIsNotNone(probe.wait_for(True, timeout=3.0))
            self.system.supervisor("replica-3").process.kill()
            self.system.supervisor("replica-3").process.wait()
            self.assertIsNotNone(probe.wait_for(False, timeout=3.0))
            self.assertIn(False, transitions)
        finally:
            probe.stop()

    def test_wait_for_times_out_when_condition_never_met(self):
        probe = HealthProbe("http://127.0.0.1:19199", poll_interval=0.02).start()  # nothing listening
        try:
            self.assertIsNone(probe.wait_for(True, timeout=0.2))
        finally:
            probe.stop()


if __name__ == "__main__":
    unittest.main()
