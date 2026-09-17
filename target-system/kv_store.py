"""A minimal single-instance, in-memory KV store — the target system's one stateful
component (spec: "3 replicas of a stateless API... plus one stateful component such as
a small key-value store"). Deliberately not replicated: a kill of this component is a
genuine full outage, unlike killing one of three interchangeable API replicas — that
asymmetry is intentional and is what the blast-radius metric is meant to surface.
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class KVHandler(BaseHTTPRequestHandler):
    store: dict = {}
    lock = threading.Lock()

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"status": "healthy", "pid": os.getpid(), "keys": len(self.store)})
            return
        if self.path.startswith("/kv/"):
            key = self.path[len("/kv/"):]
            with self.lock:
                if key in self.store:
                    self._write_json(200, {"key": key, "value": self.store[key]})
                else:
                    self._write_json(404, {"error": "no such key"})
            return
        self._write_json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.startswith("/kv/"):
            key = self.path[len("/kv/"):]
            length = int(self.headers.get("Content-Length", 0))
            value = self.rfile.read(length).decode("utf-8")
            with self.lock:
                self.store[key] = value
            self._write_json(200, {"key": key, "value": value})
            return
        self._write_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Single-instance stateful KV store")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), KVHandler)
    print(f"kv-store listening on {args.port} pid={os.getpid()}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
