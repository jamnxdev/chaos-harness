"""A minimal stateless API replica used as one of the chaos harness's fault targets.

Deliberately trivial: the interesting engineering in this project is the harness
(injectors/scheduler/recorder), not the target application. Two endpoints only:
GET /health (liveness) and GET /work (simulates a unit of request handling so the
load balancer has something real to proxy).
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ReplicaHandler(BaseHTTPRequestHandler):
    replica_id = None

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {
                "status": "healthy",
                "replica_id": self.replica_id,
                "pid": os.getpid(),
            })
        elif self.path == "/work":
            # Stand-in for real request handling latency.
            time.sleep(0.001)
            self._write_json(200, {
                "replica_id": self.replica_id,
                "pid": os.getpid(),
                "result": "ok",
            })
        else:
            self._write_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # silence default stderr access logging; the harness has its own recorder


def make_handler(replica_id: int):
    class _Handler(ReplicaHandler):
        pass
    _Handler.replica_id = replica_id
    return _Handler


def main():
    parser = argparse.ArgumentParser(description="Stateless API replica")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--id", type=int, required=True, dest="replica_id")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.replica_id))
    print(f"replica {args.replica_id} listening on {args.host}:{args.port} pid={os.getpid()}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
