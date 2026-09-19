"""Exercises real netns + tc netem / real link-down against the real Docker-based
target system -- no simulated network failure anywhere here. Requires the scoped
passwordless sudo for `ip`/`tc` set up during the Docker migration (see the
implementation log); if that isn't present these tests will fail loudly with a
"a password is required" error rather than silently skipping, which is deliberate --
a chaos harness whose network fault injector silently no-ops is worse than one that
fails loudly.
"""
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.injectors.network_impairment import NetworkImpairmentInjector
from harness.probe import HealthProbe
from harness.recorder import ResultRecorder
from harness.trial_runner import TrialRunner


class NetworkImpairmentInjectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem(restart_delay=0.02).start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def test_partition_is_detected_and_heals_after_duration(self):
        injector = NetworkImpairmentInjector(self.system, mode="partition", duration_s=1.0)
        try:
            probe = HealthProbe(self.system.component_url("replica-1"), poll_interval=0.05).start()
            try:
                self.assertIsNotNone(probe.wait_for(True, timeout=3.0))
                result = injector.inject("replica-1")
                self.assertEqual(result.fault_type, "network-partition")

                first_unhealthy_at = probe.wait_for(False, timeout=2.0)
                self.assertIsNotNone(first_unhealthy_at, "probe never saw the partitioned replica as unhealthy")

                recovered_at = probe.wait_for(True, timeout=3.0)
                self.assertIsNotNone(recovered_at, "replica never came back healthy after the partition healed")
                self.assertGreaterEqual(recovered_at, result.injected_at + injector.duration_s)
            finally:
                probe.stop()
        finally:
            injector.heal_all()

    def test_full_trial_via_trial_runner_records_partition_recovery(self):
        injector = NetworkImpairmentInjector(self.system, mode="partition", duration_s=1.0)
        tmpdir = tempfile.TemporaryDirectory()
        try:
            recorder = ResultRecorder(Path(tmpdir.name) / "results.db")
            try:
                runner = TrialRunner(
                    self.system, injector, recorder, run_id="test-run",
                    detection_timeout=2.0, recovery_timeout=4.0, stabilization_pause=0.2,
                )
                record = runner.run_trial("replica-2")
                self.assertEqual(record.fault_type, "network-partition")
                self.assertTrue(record.detected)
                self.assertTrue(record.recovered)
                self.assertEqual(len(recorder.trials_for("network-partition")), 1)
            finally:
                recorder.close()
        finally:
            injector.heal_all()
            tmpdir.cleanup()

    def test_latency_injection_measurably_slows_requests_without_full_outage(self):
        injector = NetworkImpairmentInjector(
            self.system, mode="latency", delay_ms=250, duration_s=2.0
        )
        try:
            url = f"{self.system.component_url('replica-3')}/health"

            def timed_get():
                start = time.monotonic()
                with urllib.request.urlopen(url, timeout=3.0) as resp:
                    resp.read()
                return time.monotonic() - start

            baseline = timed_get()
            self.assertLess(baseline, 0.1)

            injector.inject("replica-3")
            time.sleep(0.1)  # let the qdisc take effect
            impaired = timed_get()
            self.assertGreater(impaired, 0.2, "netem delay did not measurably slow the request")

            injector.heal_all()
            deadline = time.monotonic() + 3.0
            healed = None
            while time.monotonic() < deadline:
                healed = timed_get()
                if healed < 0.1:
                    break
                time.sleep(0.1)
            self.assertLess(healed, 0.1, "latency did not return to baseline after healing")
        finally:
            injector.heal_all()


if __name__ == "__main__":
    unittest.main()
