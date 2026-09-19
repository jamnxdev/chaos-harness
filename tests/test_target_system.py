"""End-to-end sanity tests against the real target system: actually builds and starts
the Docker containers, hits real published ports, actually tears them down. No mocking
of the target system itself -- if this were faked, the whole "real fault injection"
premise of the project would be undermined before the harness even gets involved.

Container start/stop is comparatively slow, so the whole stack is brought up once per
test class (setUpClass/tearDownClass) rather than per test method.
"""
import json
import time
import unittest
import urllib.error
import urllib.request

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem


def get_json(url, timeout=1.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


class TargetSystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = TargetSystem().start()

    @classmethod
    def tearDownClass(cls):
        cls.system.stop()

    def test_each_replica_reports_its_own_id(self):
        for i, name in enumerate(["replica-1", "replica-2", "replica-3"], start=1):
            status, body = get_json(f"{self.system.component_url(name)}/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["replica_id"], i)

    def test_kv_store_put_then_get_round_trips(self):
        url = f"{self.system.component_url('kv-store')}/kv/foo"
        req = urllib.request.Request(url, data=b"bar", method="PUT")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            self.assertEqual(resp.status, 200)
        status, body = get_json(url)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], "bar")

    def test_kv_store_missing_key_returns_404(self):
        try:
            get_json(f"{self.system.component_url('kv-store')}/kv/nope")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_load_balancer_proxies_work_requests_to_a_backend(self):
        status, body = get_json(f"{self.system.component_url('load-balancer')}/work")
        self.assertEqual(status, 200)
        self.assertIn(body["replica_id"], (1, 2, 3))

    def test_load_balancer_round_robins_across_all_replicas(self):
        seen = set()
        for _ in range(30):
            _, body = get_json(f"{self.system.component_url('load-balancer')}/work")
            seen.add(body["replica_id"])
        self.assertEqual(seen, {1, 2, 3})

    def test_container_pid_is_a_real_positive_host_pid(self):
        for name in ["replica-1", "replica-2", "replica-3", "kv-store", "load-balancer"]:
            self.assertGreater(self.system.pid(name), 0)


class TargetSystemStopTest(unittest.TestCase):
    """A dedicated start/stop cycle, separate from the shared class-level fixture
    above, specifically to prove `stop()` actually removes every container."""

    def test_stop_removes_every_container(self):
        import subprocess
        system = TargetSystem().start()
        try:
            names = ["replica-1", "replica-2", "replica-3", "kv-store", "load-balancer"]
            for name in names:
                self.assertGreater(system.pid(name), 0)
        finally:
            system.stop()

        for name in ["replica-1", "replica-2", "replica-3", "kv-store", "load-balancer"]:
            result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, f"{name} should no longer exist after stop()")


if __name__ == "__main__":
    unittest.main()
