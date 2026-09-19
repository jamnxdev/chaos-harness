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


class ContainerSupervisorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem(restart_delay=0.02).start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def test_killed_container_is_restarted_with_a_new_pid(self):
        sup = self.system.supervisor("replica-1")
        original_pid = sup.pid
        real_kill("replica-1")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if sup.pid != original_pid:
                    break
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(0.05)

        self.assertNotEqual(sup.pid, original_pid, "supervisor never respawned the killed container")
        self.assertGreaterEqual(sup.restart_count, 1)
        HealthProbe(self.system.component_url("replica-1")).start().wait_for(True, timeout=5.0)

    def test_restarted_container_answers_health_again(self):
        real_kill("replica-2")
        healthy_at = HealthProbe(self.system.component_url("replica-2"), poll_interval=0.02).start().wait_for(
            True, timeout=5.0
        )
        self.assertIsNotNone(healthy_at, "restarted replica never answered /health again")


if __name__ == "__main__":
    unittest.main()
