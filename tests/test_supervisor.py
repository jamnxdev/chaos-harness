import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem

TEST_REPLICA_PORTS = (19201, 19202, 19203)
TEST_KV_PORT = 19210
TEST_LB_PORT = 19200


class ComponentSupervisorTest(unittest.TestCase):
    def setUp(self):
        self.system = TargetSystem(TEST_REPLICA_PORTS, TEST_KV_PORT, TEST_LB_PORT, restart_delay=0.02).start()

    def tearDown(self):
        self.system.stop()

    def test_killed_process_is_restarted_with_a_new_pid(self):
        sup = self.system.supervisor("replica-1")
        original_pid = sup.pid
        original_process = sup.process
        original_process.kill()
        original_process.wait()

        deadline = time.monotonic() + 3.0
        while sup.pid == original_pid and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertNotEqual(sup.pid, original_pid, "supervisor never respawned the killed process")
        self.assertEqual(sup.restart_count, 1)

    def test_stop_does_not_trigger_a_restart(self):
        sup = self.system.supervisor("kv-store")
        sup.stop()
        time.sleep(0.3)  # long enough that an incorrect auto-restart would have happened
        self.assertIsNotNone(sup.process.poll(), "process should be dead, not respawned, after stop()")
        self.assertEqual(sup.restart_count, 0)

    def test_restarted_replica_answers_health_again(self):
        import urllib.request
        sup = self.system.supervisor("replica-2")
        sup.process.kill()
        sup.process.wait()

        url = f"{self.system.component_url('replica-2')}/health"
        deadline = time.monotonic() + 3.0
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.3) as resp:
                    if resp.status == 200:
                        return
            except OSError as exc:
                last_error = exc
            time.sleep(0.05)
        self.fail(f"restarted replica never answered /health again: {last_error}")


if __name__ == "__main__":
    unittest.main()
