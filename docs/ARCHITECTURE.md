# Architecture

## Overview

Chaos Harness has two independent halves living in the same repo:

1. **The target system** (`target-system/`) — a small, disposable distributed
   system to break. Three stateless API replicas behind a round-robin load
   balancer, plus one deliberately-unreplicated stateful KV store.
2. **The harness** (`harness/`) — everything that injects real faults into
   the target system, watches how it responds, and records what happened.

The harness never simulates a failure. Every fault is real: a real
`SIGKILL`, a real `tc netem` delay applied to a real network namespace, real
`stress-ng` load fighting a real cgroup limit. The project's only claim to
credibility is that its numbers describe what actually happened, not what a
model predicted would happen — see [Findings](#findings) for what that
turned up.

```
                      ┌───────────────────────────────────────────┐
                      │              target-system/                │
                      │                                             │
   client ──▶ load-balancer ──▶ replica-1 / replica-2 / replica-3   │
                      │                                             │
                      │         kv-store  (singleton, no LB)        │
                      └───────────────────────────────────────────┘
                                       ▲
                      inject fault     │  poll /health
                      ┌────────────────┴────────────────┐
                      │              harness/            │
                      │                                   │
                      │  injectors/        HealthProbe     │
                      │   process_kill          │          │
                      │   network_impairment     │          │
                      │   resource_pressure       ▼          │
                      │                       TrialRunner    │
                      │                            │          │
                      │  ExperimentScheduler ──────┘          │
                      │                            │          │
                      │                    ResultRecorder     │
                      │                            │          │
                      │                     results/results.db│
                      └───────────────────────────────────────┘
```

## Target system (`target-system/`)

| Component | File | Role |
|---|---|---|
| API replica × 3 | `api_replica.py` | Stateless `ThreadingHTTPServer`. `GET /health`, `GET /work`. Killing one is meant to be near-invisible to a caller going through the load balancer. |
| KV store | `kv_store.py` | Single instance, in-memory `dict` behind a `threading.Lock`. `GET/PUT /kv/<key>`. **Deliberately not replicated** — see [Why the KV store is a single point of failure](#why-the-kv-store-is-a-single-point-of-failure). |
| Load balancer | `load_balancer.py` | Round-robin reverse proxy over the 3 replicas. Reacts passively to failed proxied requests (marks a backend down for a cooldown window) rather than running its own background health-checking, which would just duplicate `HealthProbe`. |

Everything here is stdlib-only Python (`http.server`, `urllib`) — see
[Why stdlib-only](#why-stdlib-only). All five components run as separate
Docker containers, one shared image (`target-system/Dockerfile`), started and
stopped by `docker-compose.yml` or programmatically by `harness/launcher.py`.

### Why the KV store is a single point of failure

The API tier is replicated on purpose, and the KV store is not, on purpose.
The project's fault scenarios are only interesting if blast radius actually
differs between components: killing a replica should be a non-event (the load
balancer routes around it); killing the KV store should be a real, visible
outage. Making the KV store redundant too would have flattened that
asymmetry and made every fault type look the same in the results.

## Harness (`harness/`)

### `TargetSystem` (`launcher.py`)

Owns the lifecycle of all five target containers: starts them, exposes
`component_url(name)` / `pid(name)` lookups the rest of the harness needs, and
wraps every component in a `ContainerSupervisor` (see below) so a component
killed by a fault injector actually comes back instead of staying dead
forever — without a restart mechanism, "recovery time" would be meaningless to
measure.

`start()` waits for each component's *real* health (`/health` returning `200`
through `HealthProbe`), not just the container runtime reporting `Running` —
a container can be `Running` before its Python process inside has finished
opening its listening socket, and treating those as equivalent produced flaky
false starts early on.

### `ContainerSupervisor` (`container_supervisor.py`)

Watches the Docker event stream (`docker events --filter event=die`) for one
container and issues `docker start` itself the moment it dies, rather than
relying on Docker's own `restart: unless-stopped` policy.

This wasn't a stylistic choice — it's a documented discovery. Testing showed
`restart: unless-stopped` does not reliably fire in this environment: a
container `SIGKILL`ed via `docker kill` sat at `Exited`,
`State.Restarting=false`, for 10+ seconds with no restart, across repeated
trials, while the exact same container restarted instantly via a manual
`docker start`. Rather than trust an opaque platform mechanism that
demonstrably doesn't work here, the harness watches for the death event and
restarts the container itself — the same "measure, don't assume" discipline
the whole project applies to fault injection, turned on its own
infrastructure.

### `HealthProbe` (`probe.py`)

A background thread polling one component's `/health` endpoint on a fixed
interval, tracking `is_healthy` and firing an `on_transition` callback exactly
when that status flips. `wait_for(healthy, timeout)` is the building block
every timing measurement in the harness is made of: "detection latency" and
"recovery time" are nothing more than two transition events read off this
probe, bounded by a hard deadline so a fault that never resolves can never
hang the whole harness.

**Known limitation, and the source of the project's headline finding:**
health here is binary — the probe only knows "connects and returns `200`" vs.
everything else. A component that's alive but badly degraded (e.g. under CPU
pressure) can still answer `/health` fast enough to read as fully healthy.
See [Finding 1](#finding-1-the-health-probe-cannot-see-resource-pressure-at-all)
below for what that costs in practice.

### Fault injectors (`injectors/`)

All three implement the same interface (`injectors/base.py`):

```python
class FaultInjector(ABC):
    fault_type: str
    def inject(self, target_component: str, **kwargs) -> InjectionResult: ...
```

`inject()` applies the fault for real and returns immediately — it does not
wait for or manage recovery itself; that's `TrialRunner`'s job, watching the
probe. This uniform shape is what lets `ExperimentScheduler` drive
process-kill, network-impairment, and resource-pressure batches identically
without branching on fault type.

#### `ProcessKillInjector`

`docker kill --signal=SIGKILL <container>`. The simplest fault, and the
baseline every other fault type's timing is compared against.

#### `NetworkImpairmentInjector`

Two distinct fault types sharing one injector class, matching the project's
own separation of these as different failure scenarios:

- **`partition`** — `ip link set eth0 down` inside the container's network
  namespace: hard, total, bidirectional loss of connectivity.
- **`latency`** — `tc qdisc ... netem delay/loss`: degraded but still
  reachable.

The container's netns is reached via `ip netns attach <label> <pid>`
(bind-mounting the container's existing `/proc/<pid>/ns/net` under
`/run/netns/<label>`) — this works against any container runtime's PID, not
just Docker's, and needs no separate `mkdir`/`ln` privilege since `ip netns
attach` handles that internally. Full sudoers setup:
[SETUP.md](SETUP.md#4-grant-the-network-impairment-injectors-sudo-scope).

See [Finding 3](#finding-3-partition-and-latency-are-indistinguishable-at-current-settings)
for what the current probe design can and can't tell these two fault types
apart on.

#### `ResourcePressureInjector`

Runs real `stress-ng` inside the target container via `docker exec -d`
(detached, so `inject()` returns immediately per the interface contract),
contending against that container's own real cgroup CPU/memory limits — the
`cpus` / `mem_limit` values set per-service in `docker-compose.yml`. Not a
sleep()-based simulation, and not pressure applied to the whole host: each
container genuinely cannot exceed its own limit, so `stress-ng` is fighting a
real, finite ceiling.

Needs no explicit `heal()` step — `stress-ng --timeout <duration_s>s` stops
itself.

One sharp edge worth knowing about: over-limit **memory** pressure can
OOM-kill the `stress-ng` process itself rather than the target — a real
false-negative scenario in its own right (the injector "succeeds" from the
harness's point of view, but the intended pressure never actually landed),
not just a hypothetical one. Tune `vm_bytes` with the container's `mem_limit`
in mind if you're relying on sustained pressure actually being applied for
the full duration.

### `TrialRunner` (`trial_runner.py`)

Ties injector + probe + recorder together into one trial:

1. Confirm the target is healthy *before* injecting anything — a trial
   started against an already-unhealthy component would produce a
   meaningless or even negative detection latency, so this step refuses to
   proceed if the target isn't provably healthy first.
2. Inject the fault.
3. Poll for the first unhealthy transition → `detection_latency_ms`.
4. If detected, poll for the return to healthy → `recovery_time_ms`.
5. Record the trial (`ResultRecorder`).
6. A stabilization pause before returning, so one trial's aftermath can't
   contaminate the next trial's baseline-healthy check.

Both polling steps have hard timeouts. A fault that's never detected, or
detected but never recovers, records as `detected=False` / `recovered=False`
with `NULL` latencies — not a fabricated `0` and not a hang.

### `ExperimentScheduler` (`scheduler.py`)

Runs a fixed plan of experiments (injector × target component × trial count)
back-to-back, unattended, sequentially — never concurrently. Concurrent faults
against the same target system would contaminate each other's recovery
measurements, so batches always run one at a time even though it costs wall
clock time. This is what turns a single manually-triggered trial into the
≥20-trials-per-fault-type batches the project's results depend on.

### `ResultRecorder` (`recorder.py`)

SQLite (stdlib `sqlite3`, no dependency), one `trials` row per trial: run id,
fault type, target component, injection timestamp, `detected` / `recovered`
booleans, nullable `detection_latency_ms` / `recovery_time_ms`. Chosen over a
flat CSV specifically because batch analysis wants to filter/group by fault
type and component, which SQL makes trivial and CSV would mean re-parsing
every time.

`results/results.db` is gitignored — it's treated as reproducible-on-demand
via `run-experiments.sh`, not as data to commit directly. The point of the
harness is the pipeline that produces the numbers, not a frozen snapshot of
one run.

## Design decisions

### Why stdlib-only

The target system and harness use no third-party Python packages —
`http.server`, `urllib`, `sqlite3`, `subprocess`, `threading`, `dataclasses`
only. This started as a constraint (no working `pip`/`venv` on the original
development box) and was kept deliberately once removed: it means `git clone`
+ a Python 3.12 interpreter is the entire dependency surface for running the
harness itself, with nothing to pin and nothing to drift. The target system's
container image does install one non-Python dependency, `stress-ng`, because
that's the actual load-generation tool the resource-pressure injector needs —
there's no meaningful stdlib substitute for it.

### Why the target system runs on Docker containers, not bare processes

The target system originally ran as plain OS `subprocess.Popen` children
(no Docker available in the original environment). It was migrated to Docker
containers once Docker became available, for two reasons directly tied to
what the harness needs to demonstrate:

- **Resource-pressure needs a real, finite ceiling to contend against.**
  `stress-ng` fighting an unbounded host process doesn't produce a meaningful
  "pressure vs. limit" result; a container with a real cgroup `cpus`/
  `mem_limit` does.
- **Network-impairment needs a clean, addressable network namespace per
  component**, which a container gives you for free and a bare process
  sharing the host's netns does not.

This migration is also the direct cause of
[Finding 2](#finding-2-containerization-has-a-real-measured-latency-cost) —
the same fault type got meaningfully slower once measured through the
container-based architecture, and that cost is treated here as a real,
citable result of the migration rather than noise to explain away.

### Why `ContainerSupervisor` doesn't trust `restart: unless-stopped`

Covered above under [`ContainerSupervisor`](#containersupervisor-container_supervisorpy) —
included here too because it's as much a design decision (never trust an
unverified platform guarantee where the project's own numbers depend on it)
as it is an implementation detail.

## Findings

These are the results of the full ≥20-trials-per-fault-type batch
(`run-experiments.sh` → `results/results.db`, 120 trials total across 6
fault-type/component combinations), reproducible by anyone who follows
[SETUP.md](SETUP.md).

```
fault_type           component      n  detected  recovered
process-kill         replica-1     20        20         20
process-kill         kv-store      20        20         20
network-partition    replica-2     20        20         20
network-latency      replica-2     20        20         20
cpu-pressure         replica-3     20         0          0
memory-pressure      replica-3     20         0          0
```

```
process-kill / replica-1:    detection_ms median=233.72 p90=261.96  |  recovery_ms median=835.44 p90=871.87
process-kill / kv-store:     detection_ms median=233.00 p90=251.23  |  recovery_ms median=823.64 p90=839.37
network-partition/replica-2: detection_ms median=355.04 p90=357.90  |  recovery_ms median=1346.40 p90=1348.77
network-latency / replica-2: detection_ms median=355.48 p90=358.88  |  recovery_ms median=1345.08 p90=1350.30
cpu-pressure / replica-3:    detection_ms none (never detected)     |  recovery_ms none (never recovered)
memory-pressure / replica-3: detection_ms none (never detected)     |  recovery_ms none (never recovered)
```

### Finding 1: the health probe cannot see resource-pressure at all

**Cpu-pressure and memory-pressure were detected in 0 of 20 trials each.**
This is not a bug in the injector or the probe — it is the real, repeatable
behavior of a binary HTTP health check against genuine, measured resource
contention. The `/health` handler is cheap enough that it doesn't compete
meaningfully for CPU or memory, so it keeps answering fast and green even
while the container is genuinely, measurably constrained.

Stated as a metric: **at these stress levels, with this probe design, the
false-negative rate for resource-pressure faults is 100%.** That's the
project's headline result — a concrete, quantified answer to "can a naive
health check see this class of failure," backed by a full 20-trial batch per
fault type rather than a hunch or a single manual run.

### Finding 2: containerization has a real, measured latency cost

Process-kill detection/recovery latency is markedly higher on the
Docker-based target system than on the original plain-process version of the
same target and same probe: detection rose from ~20ms median to ~234ms
median (~10×), recovery from ~100ms to ~825-835ms median (~8×).

This isn't the fault injection mechanism getting slower — it's two
infrastructure layers now sitting in the path that weren't there before:
Docker's own port-publishing (NAT from the host's published port to the
container's internal socket) takes measurably longer to reflect a dead
backend than a bare OS process's socket simply closing, and `ContainerSupervisor`
issuing a real `docker start` (with all of dockerd's own bookkeeping) costs
more than a bare `fork`/`exec` of a Python interpreter. Both are real,
measured costs of a container-based architecture versus a plain-process one —
not defects, and not a general claim, but a concrete before/after number from
the same project's own history.

### Finding 3: partition and latency are indistinguishable at current settings

`network-partition` (hard link-down) and `network-latency` (250ms `netem`
delay) land on statistically indistinguishable numbers: ~355ms detection /
~1346ms recovery for both. With the probe's current ~0.3s connect timeout, a
250ms one-way delay crosses that threshold just as reliably as a hard
link-down does — so from the probe's point of view, "fully down" and "badly
degraded" currently look identical.

This is a known gap in the experimental design, not a bug: distinguishing the
two would need either a probe with a shorter timeout relative to the injected
delay, or a genuinely degraded-but-still-fast-enough latency value (e.g.
100ms instead of 250ms) chosen specifically to land under the probe's
timeout. Left as a natural next experiment rather than solved here.

## What's intentionally out of scope

- **A statistical report generator** (histograms, per-fault charts) —
  `results/results.db` has the raw data; `sqlite3` queries and the numbers
  above cover the current analysis needs without adding a charting
  dependency.
- **Degraded-vs-down health checks** — the probe is deliberately binary today
  (see Finding 1); a richer health signal is the natural next step, not
  something this pass tries to retrofit.
- **Concurrent/compound fault injection** — the scheduler runs experiments
  strictly sequentially by design (see [`ExperimentScheduler`](#experimentscheduler-schedulerpy)),
  so cascading/compound-failure scenarios are a deliberately separate future
  direction, not an oversight.
