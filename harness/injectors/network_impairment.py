"""Real network-impairment fault: applies actual `tc netem` delay/loss, or a hard
link-down partition, inside the target container's own network namespace.

The container's netns is reached via `ip netns attach <label> <pid>` (bind-mounts the
container's existing `/proc/<pid>/ns/net` under `/run/netns/<label>`, which `ip
netns exec`/`tc` can then address by name) -- this avoids needing any extra
`mkdir`/`ln` privilege beyond `ip` itself, since `ip netns attach` handles that
internally. Requires passwordless sudo scoped to exactly `/usr/sbin/ip` and
`/usr/sbin/tc` (see the implementation log's Docker-migration entry for the sudoers
setup) -- `ip netns exec <label> tc ...`/`ip netns exec <label> ip ...` both run as
root because `ip netns exec` itself runs as root under sudo and simply execs its
argument in that namespace, inheriting the privilege; no second sudo call is needed
for the child command.

Two distinct fault types, matching the spec's own separation of these as different
failure scenarios:
- "partition": `ip link set eth0 down` inside the container's netns -- a hard,
  total loss of connectivity in both directions, not just packet loss.
- "latency": `tc qdisc ... netem delay/loss` -- degraded but still reachable.
"""
import subprocess
import threading

from harness.injectors.base import FaultInjector, InjectionResult
from harness.launcher import TargetSystem

SUDO = ["sudo", "-n"]


def _netns_label(target_component: str, pid: int) -> str:
    return f"chaos-{target_component}-{pid}"


class NetworkImpairmentInjector(FaultInjector):
    def __init__(self, target_system: TargetSystem, mode: str = "partition",
                 delay_ms: int = 200, loss_percent: int = 0, duration_s: float = 2.0):
        if mode not in ("partition", "latency"):
            raise ValueError(f"unknown mode: {mode!r}")
        self.target_system = target_system
        self.mode = mode
        self.delay_ms = delay_ms
        self.loss_percent = loss_percent
        self.duration_s = duration_s
        self.fault_type = "network-partition" if mode == "partition" else "network-latency"
        self._active_labels: dict[str, str] = {}
        self._heal_timers: dict[str, threading.Timer] = {}

    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        duration_s = kwargs.get("duration_s", self.duration_s)
        pid = self.target_system.pid(target_component)
        label = _netns_label(target_component, pid)
        injected_at = self.now()

        subprocess.run([*SUDO, "ip", "netns", "attach", label, str(pid)], check=True)
        if self.mode == "partition":
            subprocess.run(
                [*SUDO, "ip", "netns", "exec", label, "ip", "link", "set", "eth0", "down"], check=True
            )
        else:
            netem_args = ["delay", f"{self.delay_ms}ms"]
            if self.loss_percent:
                netem_args += ["loss", f"{self.loss_percent}%"]
            subprocess.run(
                [*SUDO, "ip", "netns", "exec", label, "tc", "qdisc", "add", "dev", "eth0",
                 "root", "netem", *netem_args], check=True
            )

        self._active_labels[target_component] = label
        timer = threading.Timer(duration_s, self.heal, [target_component])
        timer.daemon = True
        self._heal_timers[target_component] = timer
        timer.start()

        return InjectionResult(fault_type=self.fault_type, target_component=target_component, injected_at=injected_at)

    def heal(self, target_component: str):
        """Removes the impairment and tears down the attached netns label. Called
        automatically by the `duration_s` timer, but also safe to call directly
        (e.g. test cleanup) -- idempotent if nothing is active for this component."""
        label = self._active_labels.pop(target_component, None)
        timer = self._heal_timers.pop(target_component, None)
        if timer is not None:
            timer.cancel()
        if label is None:
            return
        subprocess.run([*SUDO, "ip", "netns", "exec", label, "ip", "link", "set", "eth0", "up"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([*SUDO, "ip", "netns", "exec", label, "tc", "qdisc", "del", "dev", "eth0", "root"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([*SUDO, "ip", "netns", "delete", label],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def heal_all(self):
        for target_component in list(self._active_labels):
            self.heal(target_component)
