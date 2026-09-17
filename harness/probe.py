"""Health probe: polls a component's /health endpoint at a fixed interval on its own
background thread and reports healthy/unhealthy transitions via a callback.

This is deliberately a real HTTP GET against a real socket — the same mechanism the
recovery-timeline recorder relies on to know when a fault was first detected and when
the target recovered. Distinguishing "unhealthy" from "healthy" here is currently
binary (connects and returns 200 vs. everything else: connection refused, timeout,
non-200) — the spec's harder "unhealthy vs. degraded" distinction is deliberately not
attempted yet; see the false-negative-rate metric notes in the implementation log for
where that gap is expected to bite.
"""
import threading
import time
import urllib.error
import urllib.request

DEFAULT_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_TIMEOUT_SECONDS = 0.3


class HealthProbe:
    def __init__(self, url: str, on_transition=None,
                 poll_interval=DEFAULT_POLL_INTERVAL_SECONDS, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.url = url.rstrip("/") + "/health"
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._on_transition = on_transition
        self._healthy = None  # None = not yet probed
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    def _check_once(self) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            return False

    def _run(self):
        while not self._stop_event.is_set():
            now = time.monotonic()
            healthy = self._check_once()
            with self._lock:
                previous = self._healthy
                self._healthy = healthy
            if previous is not None and previous != healthy and self._on_transition is not None:
                self._on_transition(healthy, now)
            self._stop_event.wait(self.poll_interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def is_healthy(self):
        with self._lock:
            return self._healthy

    def wait_for(self, healthy: bool, timeout: float) -> float | None:
        """Blocks until is_healthy == `healthy`, or `timeout` seconds elapse.
        Returns the monotonic timestamp of the observation, or None on timeout.
        Used by the trial runner instead of relying solely on the transition
        callback, so a trial can have a hard deadline and never hang forever."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_healthy == healthy:
                return time.monotonic()
            time.sleep(min(self.poll_interval, 0.02))
        return None
