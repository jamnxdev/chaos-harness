"""Exercises the scheduler against the real target system with a small, fast plan
(process-kill only, few trials) -- Day 5's actual >=20-trial, multi-fault-type plan
reuses this exact class unchanged, just with a bigger plan and longer to run."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem
from harness.injectors.process_kill import ProcessKillInjector
from harness.recorder import ResultRecorder
from harness.scheduler import ExperimentScheduler


class ExperimentSchedulerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem(restart_delay=0.02).start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.recorder = ResultRecorder(Path(self.tmpdir.name) / "results.db")

    def tearDown(self):
        self.recorder.close()
        self.tmpdir.cleanup()

    def test_runs_multiple_experiments_in_order_and_records_all_trials(self):
        injector = ProcessKillInjector(self.system)
        scheduler = ExperimentScheduler(
            self.system, self.recorder, run_id="scheduler-test",
            detection_timeout=2.0, recovery_timeout=5.0, stabilization_pause=0.1,
        )
        scheduler.add(injector, "replica-1", n_trials=2)
        scheduler.add(injector, "kv-store", n_trials=2)

        results = scheduler.run_all()

        self.assertEqual(len(results[("process-kill", "replica-1")]), 2)
        self.assertEqual(len(results[("process-kill", "kv-store")]), 2)
        self.assertTrue(all(r.detected and r.recovered for records in results.values() for r in records))

        rows = self.recorder.all_trials()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(1 for r in rows if r["target_component"] == "replica-1"), 2)
        self.assertEqual(sum(1 for r in rows if r["target_component"] == "kv-store"), 2)


if __name__ == "__main__":
    unittest.main()
