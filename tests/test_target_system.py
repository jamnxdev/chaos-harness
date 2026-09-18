"""End-to-end sanity tests against the real target system: actually starts the
subprocesses, hits real sockets, actually stops them. No mocking of the target
system itself — if this were faked, the whole "real fault injection" premise of the
project would be undermined before the harness even gets involved."""
import json
import time
import unittest
import urllib.error
import urllib.request

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.launcher import TargetSystem

TEST_REPLICA_PORTS = (19001, 19002, 19003)
TEST_KV_PORT = 19010
TEST_LB_PORT = 19000


def get_json(url, timeout=1.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


class TargetSystemTest(unittest.TestCase):
    def setUp(self):
        self.system = TargetSystem(TEST_REPLICA_PORTS, TEST_KV_PORT, TEST_LB_PORT).start()
        self._wait_for_all_healthy()

    def tearDown(self):
        self.system.stop()

    def _wait_for_all_healthy(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        names = ["replica-1", "replica-2", "replica-3", "kv-store", "load-balancer"]
        while time.monotonic() < deadline:
            try:
                if all(get_json(f"{self.system.component_url(n)}/health")[0] == 200 for n in names):
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("target system did not become healthy in time")

    def test_each_replica_reports_its_own_id_and_pid(self):
        for i, name in enumerate(["replica-1", "replica-2", "replica-3"], start=1):
            status, body = get_json(f"{self.system.component_url(name)}/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["replica_id"], i)
            self.assertEqual(body["pid"], self.system.pid(name))

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

    def test_stop_terminates_every_subprocess(self):
        procs = [sup.process for sup in self.system.supervisors.values()]
        self.system.stop()
        for proc in procs:
            self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
