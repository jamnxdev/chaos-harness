"""Exercises the real injector + supervisor + trial-runner path end to end against
the real (Docker-based) target system -- this is the test that proves the Day 2 data
flow described in the spec actually works, not just that each piece compiles in
isolation."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.injectors.process_kill import ProcessKillInjector
from harness.recorder import ResultRecorder
from harness.trial_runner import TrialRunner


class ProcessKillInjectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem(restart_delay=0.02).start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.recorder = ResultRecorder(Path(self.tmpdir.name) / "results.db")
        self.injector = ProcessKillInjector(self.system)
        self.runner = TrialRunner(
            self.system, self.injector, self.recorder, run_id="test-run",
            detection_timeout=3.0, recovery_timeout=8.0, stabilization_pause=0.2,
        )

    def tearDown(self):
        self.recorder.close()
        self.tmpdir.cleanup()

    def test_inject_sends_a_real_sigkill_and_a_new_container_pid_appears(self):
        target_pid_before = self.system.pid("replica-1")
        self.injector.inject("replica-1")
        deadline = time.monotonic() + 5.0
        while self.system.pid("replica-1") == target_pid_before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotEqual(self.system.pid("replica-1"), target_pid_before)

    def test_full_trial_records_detection_and_recovery(self):
        record = self.runner.run_trial("replica-1")
        self.assertEqual(record.fault_type, "process-kill")
        self.assertTrue(record.detected, "health probe never saw the killed replica as unhealthy")
        self.assertTrue(record.recovered, "health probe never saw the replica come back healthy")
        self.assertIsNotNone(record.detection_latency_ms)
        self.assertIsNotNone(record.recovery_time_ms)
        self.assertGreaterEqual(record.recovery_time_ms, record.detection_latency_ms)

        rows = self.recorder.trials_for("process-kill")
        self.assertEqual(len(rows), 1)

    def test_batch_of_trials_against_a_single_replica_all_recover(self):
        records = self.runner.run_batch("replica-2", n_trials=3)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r.detected for r in records))
        self.assertTrue(all(r.recovered for r in records))
        self.assertEqual(len(self.recorder.trials_for("process-kill")), 3)

    def test_kv_store_kill_is_also_detected_and_recovered(self):
        # The singleton stateful component -- no load balancer in front of it, so
        # this is a genuine full outage of that component, not a masked one.
        record = self.runner.run_trial("kv-store")
        self.assertTrue(record.detected)
        self.assertTrue(record.recovered)


if __name__ == "__main__":
    unittest.main()
