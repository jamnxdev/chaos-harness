"""A lightweight round-robin reverse proxy in front of the API replicas.

Passive health awareness only (no active background health-checking of its own —
that is the harness's `HealthProbe`'s job): if a proxied request to a backend fails
to connect, that backend is marked down for a short cooldown and the request is
retried against the next backend in rotation. This is what lets a single replica
kill show up as near-zero user-visible impact at the LB level while still being a
real, measured outage at that one replica's own health endpoint.
"""
import argparse
import itertools
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOWN_COOLDOWN_SECONDS = 1.0
UPSTREAM_TIMEOUT_SECONDS = 0.5


class BackendPool:
    def __init__(self, backend_ports):
        self.backend_ports = list(backend_ports)
        self._down_until = {port: 0.0 for port in self.backend_ports}
        self._cycle = itertools.cycle(self.backend_ports)
        self._lock = threading.Lock()

    def mark_down(self, port):
        with self._lock:
            self._down_until[port] = time.monotonic() + DOWN_COOLDOWN_SECONDS

    def candidates(self):
        """Yields backends in round-robin order, skipping ones in cooldown, for up
        to one full lap of the pool (never loops forever if everything is down).
        Pulls exactly one step from the shared cycle per call so consecutive calls
        continue rotating rather than each restarting from the same backend."""
        now = time.monotonic()
        with self._lock:
            start = next(self._cycle)
        port = start
        for _ in range(len(self.backend_ports)):
            if self._down_until[port] <= now:
                yield port
            with self._lock:
                port = next(self._cycle)
            if port == start:
                break


def make_handler(pool: BackendPool):
    class ProxyHandler(BaseHTTPRequestHandler):
        def _proxy(self, method):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            last_error = None
            for port in pool.candidates():
                url = f"http://127.0.0.1:{port}{self.path}"
                try:
                    req = urllib.request.Request(url, data=body, method=method)
                    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
                        payload = resp.read()
                        self.send_response(resp.status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                    pool.mark_down(port)
                    last_error = exc
                    continue
            self._write_json(503, {"error": "no healthy backend", "detail": str(last_error)})

        def _write_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._proxy("GET")

        def do_PUT(self):
            self._proxy("PUT")

        def log_message(self, fmt, *args):
            pass

    return ProxyHandler


def main():
    parser = argparse.ArgumentParser(description="Round-robin load balancer")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend-ports", type=int, nargs="+", required=True)
    args = parser.parse_args()

    pool = BackendPool(args.backend_ports)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(pool))
    print(f"load-balancer listening on {args.port} -> {args.backend_ports}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
