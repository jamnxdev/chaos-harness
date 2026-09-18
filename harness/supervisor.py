"""Auto-restarts a target-system component when its process exits unexpectedly.

A container runtime's restart policy is what would normally play this role (the spec's
own architecture assumes one). Since the target system here runs as plain OS processes
(see launcher.py's deviation note), something has to fill that role explicitly, or a
process-kill fault would have no recovery to measure at all -- the process would just
stay dead forever. `ComponentSupervisor` is that something: a single background thread
per component that blocks on `Popen.wait()` and respawns via a caller-supplied factory
whenever the exit wasn't requested by `stop()`.
"""
import subprocess
import threading
import time


class ComponentSupervisor:
    def __init__(self, name: str, spawn_fn, restart_delay: float = 0.05):
        self.name = name
        self._spawn_fn = spawn_fn
        self.restart_delay = restart_delay
        self.restart_count = 0
        self._lock = threading.Lock()
        self._intentional_stop = threading.Event()
        self._watch_thread = None
        self.process: subprocess.Popen = self._spawn_fn()

    def start(self):
        self._watch_thread = threading.Thread(target=self._watch, daemon=True)
        self._watch_thread.start()
        return self

    def _watch(self):
        while True:
            proc = self.process
            proc.wait()
            if self._intentional_stop.is_set():
                return
            time.sleep(self.restart_delay)
            with self._lock:
                self.process = self._spawn_fn()
                self.restart_count += 1

    def stop(self, timeout: float = 2.0):
        self._intentional_stop.set()
        proc = self.process
        if proc.poll() is None:
            proc.terminate()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=timeout + 1.0)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    @property
    def pid(self) -> int:
        with self._lock:
            return self.process.pid
