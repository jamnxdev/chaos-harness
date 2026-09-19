"""A lightweight round-robin reverse proxy in front of the API replicas.

Passive health awareness only (no active background health-checking of its own —
that is the harness's `HealthProbe`'s job): if a proxied request to a backend fails
to connect, that backend is marked down for a short cooldown and the request is
retried against the next backend in rotation. This is what lets a single replica
kill show up as near-zero user-visible impact at the LB level while still being a
real, measured outage at that one replica's own health endpoint.

Backends are addressed as "host:port" (container hostnames on the Docker Compose
network, e.g. "replica-1:8080"), not bare ports on localhost — each replica is its
own container with its own network namespace, not a process sharing the LB's host.
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
    def __init__(self, backends):
        self.backends = list(backends)  # "host:port" strings
        self._down_until = {b: 0.0 for b in self.backends}
        self._cycle = itertools.cycle(self.backends)
        self._lock = threading.Lock()

    def mark_down(self, backend):
        with self._lock:
            self._down_until[backend] = time.monotonic() + DOWN_COOLDOWN_SECONDS

    def candidates(self):
        """Yields backends in round-robin order, skipping ones in cooldown, for up
        to one full lap of the pool (never loops forever if everything is down).
        Pulls exactly one step from the shared cycle per call so consecutive calls
        continue rotating rather than each restarting from the same backend."""
        now = time.monotonic()
        with self._lock:
            start = next(self._cycle)
        backend = start
        for _ in range(len(self.backends)):
            if self._down_until[backend] <= now:
                yield backend
            with self._lock:
                backend = next(self._cycle)
            if backend == start:
                break


def make_handler(pool: BackendPool):
    class ProxyHandler(BaseHTTPRequestHandler):
        def _proxy(self, method):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            last_error = None
            for backend in pool.candidates():
                url = f"http://{backend}{self.path}"
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
                    pool.mark_down(backend)
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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backends", nargs="+", required=True, help="host:port pairs")
    args = parser.parse_args()

    pool = BackendPool(args.backends)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(pool))
    print(f"load-balancer listening on {args.host}:{args.port} -> {args.backends}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
