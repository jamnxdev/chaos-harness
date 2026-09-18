"""Results database: every trial's recovery timeline, persisted to SQLite (stdlib,
no dependency needed) so Day 5's full experiment batches and Day 6's report generator
have real, queryable, reproducible raw data instead of only in-memory results.
"""
import datetime
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    target_component TEXT NOT NULL,
    injected_at_utc TEXT NOT NULL,
    detected BOOLEAN NOT NULL,
    recovered BOOLEAN NOT NULL,
    detection_latency_ms REAL,
    recovery_time_ms REAL,
    notes TEXT
);
"""


@dataclass
class TrialRecord:
    run_id: str
    fault_type: str
    target_component: str
    detected: bool
    recovered: bool
    detection_latency_ms: float | None
    recovery_time_ms: float | None
    notes: str = ""


class ResultRecorder:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def record_trial(self, record: TrialRecord):
        self._conn.execute(
            """INSERT INTO trials
               (run_id, fault_type, target_component, injected_at_utc,
                detected, recovered, detection_latency_ms, recovery_time_ms, notes)
               VALUES (:run_id, :fault_type, :target_component, :injected_at_utc,
                       :detected, :recovered, :detection_latency_ms, :recovery_time_ms, :notes)""",
            {**asdict(record), "injected_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        )
        self._conn.commit()

    def trials_for(self, fault_type: str) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute(
            "SELECT * FROM trials WHERE fault_type = ? ORDER BY id", (fault_type,)
        )
        return cur.fetchall()

    def all_trials(self) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute("SELECT * FROM trials ORDER BY id")
        return cur.fetchall()

    def close(self):
        self._conn.close()
