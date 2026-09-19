"""Auto-restarts a Docker container when it dies unexpectedly.

This replaces Day 2's `ComponentSupervisor` (hand-rolled process supervision for bare
`Popen` children) now that the target system runs as Docker containers -- see the
implementation log's "Docker migration" entry for why. It was **not** built on top of
Docker's own `restart: unless-stopped` policy: testing showed that policy does not
reliably fire in this environment (verified directly -- a container SIGKILLed via
`docker kill` stayed `Exited`, `State.Restarting=false`, for 10+ seconds with no
restart, across repeated trials, while the exact same container restarted instantly
via a manual `docker start`). Rather than trust an opaque platform mechanism that
demonstrably doesn't work here, the harness watches the Docker event stream itself and
issues the restart directly -- the same "measure, don't assume" discipline this whole
project is about, just applied to its own target system's reliability instead of only
the fault injectors.
"""
import json
import subprocess
import threading
import time


class ContainerSupervisor:
    def __init__(self, name: str, restart_delay: float = 0.05):
        self.name = name
        self.restart_delay = restart_delay
        self.restart_count = 0
        self._intentional_stop = threading.Event()
        self._events_proc: subprocess.Popen | None = None
        self._watch_thread: threading.Thread | None = None

    def start(self):
        self._events_proc = subprocess.Popen(
            ["docker", "events", "--filter", f"container={self.name}",
             "--filter", "event=die", "--format", "{{json .}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self._watch_thread = threading.Thread(target=self._watch, daemon=True)
        self._watch_thread.start()
        return self

    def _watch(self):
        for line in self._events_proc.stdout:
            if self._intentional_stop.is_set():
                return
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("Action") != "die":
                continue
            time.sleep(self.restart_delay)
            subprocess.run(["docker", "start", self.name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.restart_count += 1

    def stop(self, timeout: float = 5.0):
        self._intentional_stop.set()
        if self._events_proc is not None:
            self._events_proc.terminate()
            try:
                self._events_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._events_proc.kill()
            if self._events_proc.stdout is not None:
                self._events_proc.stdout.close()
        subprocess.run(["docker", "stop", "-t", "2", self.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=timeout)

    @property
    def pid(self) -> int:
        """The container's main process's *host*-visible PID (from `State.Pid`),
        re-queried fresh on every call rather than cached -- callers that need to
        detect a restart (a process-kill injector, tests) must see the new PID
        immediately after one occurs, not a stale value from before the kill."""
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", self.name],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out)
