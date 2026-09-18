import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.recorder import ResultRecorder, TrialRecord


class ResultRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.recorder = ResultRecorder(Path(self.tmpdir.name) / "results.db")

    def tearDown(self):
        self.recorder.close()
        self.tmpdir.cleanup()

    def test_recorded_trial_round_trips_correctly(self):
        record = TrialRecord(
            run_id="run-1", fault_type="process-kill", target_component="replica-1",
            detected=True, recovered=True, detection_latency_ms=12.5, recovery_time_ms=340.1,
            notes="test",
        )
        self.recorder.record_trial(record)

        rows = self.recorder.trials_for("process-kill")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_component"], "replica-1")
        self.assertEqual(bool(rows[0]["detected"]), True)
        self.assertEqual(bool(rows[0]["recovered"]), True)
        self.assertAlmostEqual(rows[0]["detection_latency_ms"], 12.5)
        self.assertAlmostEqual(rows[0]["recovery_time_ms"], 340.1)

    def test_trials_for_filters_by_fault_type(self):
        self.recorder.record_trial(TrialRecord(
            run_id="r", fault_type="process-kill", target_component="replica-1",
            detected=True, recovered=True, detection_latency_ms=1.0, recovery_time_ms=2.0,
        ))
        self.recorder.record_trial(TrialRecord(
            run_id="r", fault_type="network-impairment", target_component="replica-1",
            detected=True, recovered=True, detection_latency_ms=1.0, recovery_time_ms=2.0,
        ))
        self.assertEqual(len(self.recorder.trials_for("process-kill")), 1)
        self.assertEqual(len(self.recorder.trials_for("network-impairment")), 1)
        self.assertEqual(len(self.recorder.all_trials()), 2)

    def test_undetected_trial_stores_null_latencies(self):
        self.recorder.record_trial(TrialRecord(
            run_id="r", fault_type="process-kill", target_component="kv-store",
            detected=False, recovered=False, detection_latency_ms=None, recovery_time_ms=None,
            notes="no unhealthy transition observed",
        ))
        row = self.recorder.trials_for("process-kill")[0]
        self.assertIsNone(row["detection_latency_ms"])
        self.assertIsNone(row["recovery_time_ms"])

    def test_recorder_persists_across_reconnect(self):
        self.recorder.record_trial(TrialRecord(
            run_id="r", fault_type="process-kill", target_component="replica-1",
            detected=True, recovered=True, detection_latency_ms=5.0, recovery_time_ms=10.0,
        ))
        db_path = self.recorder.db_path
        self.recorder.close()

        reopened = ResultRecorder(db_path)
        try:
            self.assertEqual(len(reopened.all_trials()), 1)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
