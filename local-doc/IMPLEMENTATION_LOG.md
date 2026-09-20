# Implementation Log — Chaos Engineering Harness (D1)

Running, in-depth log of what was actually built, in chronological order. Distinct from
the per-project spec (`PORTFOLIO_PROJECT_3_CHAOS_HARNESS.md`), which describes intent —
this file records what exists, decisions made along the way, and why. Updated after every
work session.

---

## 2026-09-17 — Day 1: Target system (plain-process replicas + LB + stateful KV) + health probe

### Environment reality check, done before writing any code

Before starting, checked what this VPS/dev box actually has available, since the spec's
architecture assumes Docker and root:

- **No Docker, no docker-compose installed**, and no passwordless `sudo` — can't silently
  install it either.
- **No passwordless sudo at all.** Real `ip netns`/`tc netem` (Day 3) and `stress-ng`
  normally need root.
- **`stress-ng` not installed.**
- **cgroup v2 *is* delegated** to the user's own systemd slice
  (`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/`, with `cpu`, `memory`,
  `pids` controllers all present and user-writable) — meaning Day 4's resource-pressure
  injector can use **real cgroup limits with no root at all**, no deviation needed there.
- **No `pip`, no working `venv`** (`ensurepip` missing, would itself need
  `apt install python3-venv` with sudo) — so this project is being built **stdlib-only**:
  `http.server`, `urllib`, `sqlite3`, `statistics`, `subprocess`, `threading`. The spec's
  suggested `numpy`/`pandas`/`matplotlib`/`scipy` are Day 6-7 (report generation) tools
  anyway, out of scope for the Day 1-5 work committed so far.

Raised these constraints to the user before writing code (per their instruction to ask
when unsure which way to go) rather than silently deviating. Decisions confirmed:

1. **Target system: plain OS processes, not containers.** No Docker install.
2. **Root/netns for Day 3 network-impairment**: revisit when that day is reached, rather
   than blocking Day 1-2 on it.
3. **Commit dating**: spread the 5 logical days across 5 distinct calendar dates
   (2026-09-17 → 2026-09-21) with randomized, varied commit times, rather than compressing
   everything into one real day the way `order-matching-engine`'s log did.

### Project scaffolding

- Created `chaos-harness/` as its own git repository (separate from the portfolio planning
  docs and the other project repos), `main` branch (matching the other two repos'
  convention).
- GitHub structure per the spec: `/target-system`, `/harness` (with `injectors/` inside,
  added once Day 2 needs it), `/tests`, `/results`, `/local-doc` (this log).
- No build tool/dependency manager needed (stdlib-only, see above) — no `requirements.txt`
  is being added since there is nothing to pin.

### Target system (`target-system/`)

- **`api_replica.py`** — one of the 3 stateless API replicas. `ThreadingHTTPServer` with
  two endpoints: `GET /health` (returns replica id + PID) and `GET /work` (a trivial
  simulated request, `time.sleep(0.001)` standing in for real handling latency). Takes
  `--port`/--id` so the launcher can start 3 independent instances. Stateless by
  construction: no data survives between requests, so killing and restarting one is
  behaviorally invisible to the caller as long as the load balancer routes around it.
- **`kv_store.py`** — the spec's "one stateful component." Single instance, in-memory
  `dict`, `GET/PUT /kv/<key>`, no replication. Deliberately **not** made redundant: the
  spec's own failure-scenario list implies asymmetric blast radius between the replicated
  API tier and this singleton, and that asymmetry is only real if the KV store is a true
  single point of failure. A `threading.Lock` guards the dict since `ThreadingHTTPServer`
  handles requests concurrently.
- **`load_balancer.py`** — round-robin reverse proxy in front of the 3 replicas.
  `BackendPool.candidates()` pulls one backend at a time from a shared `itertools.cycle`,
  skipping any backend currently in a 1s "down" cooldown, for at most one full lap (so it
  can never spin forever if every backend is down — same discipline as the spec's own
  "avoid a chaos experiment that never recovers and hangs the whole harness" concern,
  applied to the LB itself). On a proxied request that fails to connect, that backend is
  marked down and the next candidate is tried.
  - **Bug caught during testing**: the first version of `candidates()` took a full lap
    (`len(backend_ports)` items) from the cycle on *every* call, which meant every call
    started from the same point in the cycle and consumed a full lap each time — so every
    request ended up hitting the first backend in the pool, not round-robining at all.
    Caught immediately by `test_load_balancer_round_robins_across_all_replicas` (which
    fired 30 requests through the LB and asserted all 3 replica IDs appeared) rather than
    passing silently. Fixed by pulling exactly one step from the shared cycle per call, so
    consecutive calls continue advancing the rotation instead of each restarting from the
    same backend. Documented here rather than silently patched, per the standard set by
    the order-matching-engine log's own bug callouts.
  - The LB does **not** run its own background health-checking — that would duplicate the
    harness's `HealthProbe`. It only reacts passively to a request actually failing, which
    is enough to demonstrate the intended behavior (single replica kill → near-invisible at
    the LB; KV-store kill → real, visible outage, since there is no LB in front of it).

### Launcher (`harness/launcher.py`)

- **`TargetSystem`** — starts all 5 processes (3 replicas, KV store, load balancer) as
  plain `subprocess.Popen` children, tracks them by name (`replica-1..3`, `kv-store`,
  `load-balancer`), and exposes `component_url()`/`pid()` lookups other harness code needs.
- `stop()` is graceful-then-forced: `terminate()` every process, wait up to a shared
  deadline, `kill()` anything still alive after that — avoids leaking zombie Python
  processes across test runs, which would otherwise silently accumulate on repeated local
  test runs (a real risk noticed while iterating on the tests below, before this
  graceful/forced split existed).

### Health probe (`harness/probe.py`)

- **`HealthProbe`** — background thread polling a component's `/health` endpoint on a
  fixed interval (default 50ms), tracking `is_healthy` and firing an `on_transition`
  callback exactly when the status flips. This is the piece the Day 2+ recovery-timeline
  recorder will build on directly: "first-detected-unhealthy" and "recovered" timestamps
  are nothing more than transition events from this class.
  - `wait_for(healthy, timeout)` — a polling helper with a hard deadline, returning `None`
    on timeout instead of hanging. Added specifically so later trial-running code (Day 2+)
    can never block forever waiting for a fault that, for whatever reason, never resolves —
    directly answering the spec's own "avoiding a chaos experiment that never recovers and
    hangs the whole harness" hardest-problem callout, at the probe layer rather than leaving
    it to be solved later under time pressure.
  - Health is currently binary (connects + returns 200, or not) — the spec's harder
    "unhealthy vs. degraded" distinction is explicitly not attempted at this layer yet.
    Flagged here as the known gap behind the eventual false-negative-rate metric (Day 5+):
    a component that's alive but badly degraded (e.g. under CPU pressure) may still answer
    `/health` fast enough to read as healthy to this binary check.

### Tests

- **`test_target_system.py`** (6 cases) — starts the *real* target system as real
  subprocesses against real sockets for every test (no mocking): each replica reports its
  own id/PID correctly; KV store PUT-then-GET round-trips; a missing KV key 404s; the load
  balancer proxies a `/work` request to some backend; 30 requests through the LB touch all
  3 replica IDs (the test that caught the round-robin bug above); `stop()` actually
  terminates every subprocess (checked via `Popen.poll()` returning non-`None`).
- **`test_probe.py`** (4 cases) — also against the real target system: probe reports
  healthy once the target is up; probe detects the transition to unhealthy after a real
  `kill()` of a replica process; the `on_transition` callback fires with the correct
  boolean; `wait_for` times out (doesn't hang) when its condition is never met, checked
  against a port with nothing listening.
- Full suite: **10 tests, 0 failures** (`python3 -m unittest discover -s tests`).

### Commits (chronological)

1. `Target system: stateless API replicas, stateful KV store, round-robin load balancer`
2. `Launcher and health probe for the target system, end-to-end tests`

### Deviations from the spec, called out explicitly

- **Target system runs as plain OS processes, not containers** (no Docker on this box, no
  passwordless sudo to install it). Confirmed with the user before building. Process-kill
  is still a real `SIGKILL` against a real PID; only the container-runtime layer itself is
  swapped out, and the spec already lists that layer as a "replaceable component," not part
  of the project's stable core.
- **No external Python packages** — stdlib only, for the reasons above. Will need to be
  revisited once Day 6/7 report generation wants `matplotlib`, which will require either
  getting `pip`/`venv` working (needs `apt install python3-venv`, sudo) or hand-rolling
  simple ASCII/CSV-only output instead of real charts. Not yet a blocker for Day 1-5 work.

### Not yet built (per the 7-day plan, still ahead)

- Day 2: process-kill injector, process supervisor (auto-restart on kill, since something
  has to play the role a container runtime's restart policy would), recovery-timeline
  recording, first small batch of trials.
- Day 3: network-impairment injector (netns + netem) — needs root; will stop and ask the
  user for a scoped sudo/visudo setup when this is reached, per their earlier answer.
- Day 4: resource-pressure injector via real (delegated, non-root) cgroup v2 limits;
  scheduler to run trials unattended.
- Day 5: full experiment batches (≥20 trials per fault type); results database.
- Day 6 (out of scope for this pass): statistical report generator, re-run on a real VPS.
- Day 7 (out of scope for this pass): README, architecture diagram, blog post, CV bullets.

---

## 2026-09-18 — Day 2: Process supervisor, process-kill injector, recovery-timeline recording

### Process supervisor (`harness/supervisor.py`)

- **`ComponentSupervisor`** — the piece the target system was missing to make a
  process-kill fault meaningful at all: without a container runtime's restart policy,
  killing a process would just leave it dead forever, and there would be nothing to
  measure "recovery" against. One background thread per component blocks on
  `Popen.wait()` and, if the exit wasn't requested via `stop()`, waits a short
  `restart_delay` and respawns via a caller-supplied factory closure, tracking a
  `restart_count`.
  - `stop()` sets an "intentional stop" flag *before* terminating the process, so the
    watch thread's own `wait()` returning doesn't trigger a spurious respawn on
    shutdown — verified directly by `test_stop_does_not_trigger_a_restart` (stops a
    component, waits long enough that an incorrect auto-restart would have already
    happened, asserts it's still dead with `restart_count == 0`).
  - Only the watch thread ever calls `Popen.wait()` on a given process object — `stop()`
    only `terminate()`s and then joins the watch thread, rather than also calling
    `wait()` itself, specifically to avoid two threads racing on the same `Popen`'s
    internal wait/lock state.

### `TargetSystem` refactored to use supervisors, not bare `Popen`s

- Every component (`replica-1..3`, `kv-store`, `load-balancer`) is now wrapped in its
  own `ComponentSupervisor` instead of a raw `subprocess.Popen`. `pid(name)` now reads
  through to the supervisor's *current* process, since that PID changes after every
  restart — callers that cached a PID before this change would have gone stale silently,
  which is exactly the bug class the process-kill injector needs to avoid (see below).
- This is a breaking change to `TargetSystem`'s public shape (`processes` dict replaced
  by `supervisors`), so the Day 1 tests (`test_target_system.py`, `test_probe.py`) were
  updated in the same session to go through `system.supervisor(name).process` instead of
  the old `system.processes[name]`. Re-ran the full Day 1 suite after the refactor to
  confirm nothing regressed — still green.

### Process-kill injector (`harness/injectors/process_kill.py`)

- **`FaultInjector`** (`harness/injectors/base.py`) — a tiny common interface
  (`inject(target_component, **kwargs) -> InjectionResult`) every injector will implement,
  so the Day 4/5 scheduler can drive process-kill, network-impairment, and
  resource-pressure identically without branching on fault type.
- **`ProcessKillInjector`** — looks up the target component's *current* PID through its
  supervisor (not a PID cached earlier, for the staleness reason above) and sends a real
  `os.kill(pid, SIGKILL)`. No simulated failure anywhere in this path — the spec's own
  design-decision section is explicit that the whole project's credibility rests on this.

### Recovery-timeline recorder (`harness/recorder.py`)

- **`ResultRecorder`** — SQLite (stdlib `sqlite3`, no dependency) results database, one
  `trials` row per trial: run id, fault type, target component, wall-clock injection time,
  `detected`/`recovered` booleans, `detection_latency_ms`/`recovery_time_ms` (nullable —
  a trial where the probe never saw the fault, or never saw recovery, stores `NULL` rather
  than a fabricated number). This is the spec's "results database" MUST BUILD item, chosen
  over a flat CSV specifically because Day 5's full batches (≥20 trials × 3 fault types)
  and Day 6's report generator both want to filter/query by fault type, which SQL makes
  trivial and CSV would need re-parsing for every time.

### Trial runner (`harness/trial_runner.py`)

- **`TrialRunner`** — the literal implementation of the spec's data-flow paragraph:
  confirm the target is healthy first (refuses to run a trial against an already-unhealthy
  component, since that would produce a meaningless or even negative detection latency) →
  inject the fault → poll for the first unhealthy transition (`detection_latency_ms`) →
  if detected, poll for the return to healthy (`recovery_time_ms`) → record the trial →
  stabilization pause before returning, so one trial's aftermath can't contaminate the
  next trial's baseline-healthy check (the spec's own experimental-design requirement).
  - Both `wait_for` calls have hard timeouts (`detection_timeout`, `recovery_timeout`), so
    a fault that never resolves records as `detected=False`/`recovered=False` with a note,
    rather than hanging the whole harness — the same "never hang forever" discipline the
    probe's own `wait_for` was built with on Day 1, now applied one level up.

### Tests

- **`test_supervisor.py`** (3 cases) — a killed process gets a new PID within a few
  hundred ms and `restart_count` increments; `stop()` does not trigger a restart; a
  restarted replica answers `/health` again (not just "some process exists").
- **`test_recorder.py`** (4 cases) — a recorded trial round-trips correctly through
  SQLite; `trials_for()` filters by fault type; an undetected trial stores `NULL`
  latencies rather than `0` or a sentinel; a recorder reopened against the same DB file
  sees previously recorded rows (proving persistence, not just in-process caching).
- **`test_process_kill_injector.py`** (4 cases) — `inject()` causes a real, different PID
  to own the component's name afterward; a full trial against a replica records both
  detection and recovery with `recovery_time_ms >= detection_latency_ms`; a 5-trial batch
  against one replica all detect and recover; a kill against the singleton `kv-store` (no
  load balancer masking it) is also detected and recovered. This is the test that proves
  the whole Day 2 data flow works end to end against the real target system, not just that
  each class compiles in isolation.
- Full suite: **21 tests, 0 failures** (`python3 -m unittest discover -s tests`).

### First small batch of trials (informal, per the spec's own Day 2 scope — not the ≥20-trial statistical batches, that's Day 5)

Ran 10 process-kill trials each against `replica-1` and the singleton `kv-store`
(`restart_delay=0.02s`, probe `poll_interval=0.02s`, one-off manual script, not committed
as a permanent script since Day 5's scheduler will supersede it):

```
replica-1 (process-kill): n=10 detected=10 recovered=10
  detection_ms: median=20.18 min=20.08 max=20.31
  recovery_ms:  median=100.75 min=80.35 max=121.12
kv-store (process-kill):  n=10 detected=10 recovered=10
  detection_ms: median=20.24 min=20.13 max=20.30
  recovery_ms:  median=100.55 min=80.55 max=100.87
```

- **Detection latency clusters tightly around 20ms — exactly the probe's
  `poll_interval`.** Expected and worth flagging honestly now rather than at Day 6: with a
  20ms poll interval, the probe can only ever report "unhealthy" up to ~20ms after the
  fact, so detection latency here is really measuring "one poll interval," not the health
  check's own responsiveness. A real headline result will need either a much shorter poll
  interval or an explicit statement that detection latency is bounded below by the polling
  interval — a direct answer to the spec's own difficult-follow-up question ("how do you
  know your health probe polling interval itself isn't dominating your measured detection
  latency?").
- **Recovery time (~100ms median) is dominated by `restart_delay` (20ms) plus the new
  Python process's own startup/bind time**, not by anything the fault-injection mechanism
  itself is measuring — also worth being explicit about, since a real production restart
  policy (or a container) would have different startup overhead than a bare CPython
  interpreter cold-starting `ThreadingHTTPServer`.
- **`replica-1` and `kv-store` show statistically indistinguishable recovery numbers at
  this sample size** — expected, since both are being restarted by the identical
  supervisor mechanism; the interesting *blast-radius* difference between them (LB masks a
  replica kill vs. a KV-store kill being a full outage) isn't visible in these numbers
  because nothing in this batch measured LB-level availability yet. Flagged as a gap for
  the Day 5 full batches: worth adding an LB-availability probe running *concurrently*
  with the target-component probe, to actually capture that asymmetry.

### Commits (chronological, continued)

3. `Process supervisor with auto-restart, refactor TargetSystem to use it`
4. `Process-kill injector, SQLite results recorder, trial runner, first manual batch`

### Not yet built (per the 7-day plan, still ahead)

- Day 3: network-impairment injector (netns + netem) — needs root; will stop and ask the
  user for a scoped sudo/visudo setup when this is reached.
- Day 4: resource-pressure injector via real (delegated, non-root) cgroup v2 limits;
  scheduler to run trials unattended.
- Day 5: full experiment batches (≥20 trials per fault type, including the LB-availability
  gap noted above); results database (already built, will just accumulate more rows).
- Day 6 (out of scope for this pass): statistical report generator, re-run on a real VPS.
- Day 7 (out of scope for this pass): README, architecture diagram, blog post, CV bullets.

---

## 2026-09-19 — Docker migration: target system rebuilt on real containers

### Why this happened now, not on Day 1

Before starting Day 3 (network-impairment via netns/netem, which needs root), re-raised
the earlier Day 1 environment deviation with the user rather than quietly carrying it
forward. Confirmed answer: install Docker/`stress-ng` and set up scoped sudo properly,
so the target system matches the spec's actual architecture instead of the plain-process
substitute. The user ran, via `!`:

```
sudo apt update && sudo apt install -y docker.io docker-compose-plugin stress-ng
sudo usermod -aG docker $USER
sudo systemctl enable --now docker
echo 'jamnxdev ALL=(root) NOPASSWD: /usr/sbin/ip, /usr/sbin/tc' | sudo tee /etc/sudoers.d/chaos-harness-netns
sudo chmod 440 /etc/sudoers.d/chaos-harness-netns
sudo visudo -c
```

Verified afterward: `docker ps` reachable, `docker compose` v5.5.1 present, `stress-ng
0.17.06` installed, `sudo -n ip netns list` / `sudo -n tc qdisc show` both work
passwordless. One session-only wrinkle: the assistant's own tool shell predates the
`usermod -aG docker` change, so `docker` commands run from *this* tool session need an
`sg docker -c "..."` wrapper to pick up the new group membership; a normal terminal
opened fresh after the `usermod` does not need this. The harness code itself always
shells out to plain `docker`/`docker compose` — the wrapper is a one-off artifact of
this development session, not something baked into any committed script.

### Target system rebuilt on Docker

- **`target-system/Dockerfile`** — one shared image for all three component scripts
  (still stdlib-only, nothing to `pip install`), differentiated per service purely via
  each docker-compose service's `command` override.
- **`target-system/docker-compose.yml`** — 3 `replica-N` services + `kv-store` +
  `load-balancer`, each with an explicit `container_name` matching the component names
  already used everywhere in the harness (`replica-1`, `kv-store`, etc.), host ports
  overridable via env vars (`REPLICA1_PORT`, `KV_PORT`, ...) so tests could in principle
  run an isolated stack, though in practice the whole suite currently shares one fixed
  set of ports/names since `unittest discover` runs test modules sequentially.
- **`api_replica.py`/`kv_store.py`/`load_balancer.py`** updated to bind `0.0.0.0` instead
  of `127.0.0.1` — required for Docker's port-publishing NAT to reach the process inside
  the container's own network namespace at all; a loopback-only bind would be invisible
  from outside the container. `load_balancer.py`'s `--backend-ports` CLI flag became
  `--backends host:port ...`, since replicas are now separate containers reachable by
  Docker Compose's service-name DNS (`replica-1:8080`), not separate ports on one
  shared host.

### Discovery: Docker's own `restart: unless-stopped` policy is unreliable here

Tested directly before trusting it: started the compose stack, `docker kill
--signal=SIGKILL` a container declared `restart: unless-stopped`, then polled
`docker inspect` every second for 10+ seconds. `State.Status` stayed `exited`,
`State.Restarting` stayed `false`, `RestartCount` stayed `0` the whole time — no
automatic restart happened, even though a manual `docker start` on the same container
worked instantly. Re-ran the same test again on a different replica with the same
result. Root cause not chased further (a containerd/dockerd restart-manager
interaction specific to this box is the leading suspect, but not confirmed) — what
matters for this project is that the policy is **not reliable enough to depend on**,
and it later turned out to be actively harmful: once real trials started, Docker's
own policy occasionally *did* fire (at unpredictable delay, once observed to race
ahead of the harness's own restart), producing a container recovery whose
`restart_count` bookkeeping came from nowhere the harness could see — a real,
non-deterministic double-restart-mechanism bug caught via a flaky
`test_killed_container_is_restarted_with_a_new_pid` failure (PID changed, but
`ContainerSupervisor.restart_count` stayed `0`).

**Fix**: set every service's `restart:` to `"no"` in the compose file, so recovery is
driven by exactly one mechanism — the harness's own supervisor — and is therefore
deterministic and fully attributable. This is the same "measure, don't assume"
discipline the whole project is about, turned on the target system's own
infrastructure instead of only the fault injectors.

### `ContainerSupervisor` (`harness/container_supervisor.py`) replaces Day 2's `ComponentSupervisor`

- Watches `docker events --filter container=<name> --filter event=die --format
  '{{json .}}'` on a background thread (one subprocess per component) instead of
  blocking on `Popen.wait()` (there is no `Popen` for a container's main process from
  the host's perspective in the same sense). On a `die` event, waits `restart_delay`
  then issues `docker start <name>`, incrementing `restart_count` — deliberately event
  driven rather than polling `docker inspect` in a loop, both for lower latency and to
  avoid spawning a CLI subprocess every poll tick.
  - `pid` re-queries `docker inspect -f '{{.State.Pid}}'` fresh on every access rather
    than caching — this is the *host*-visible PID of the container's PID-1 process
    (containers get their own PID namespace by default, but the process is still a
    real, killable PID from the host), so `os.kill`-style semantics still apply; the
    injector was simplified to shell out to `docker kill` directly instead, which is
    the more idiomatic way to signal a specific container regardless of its internal
    PID namespace.
- Day 2's `ComponentSupervisor` (bare-`Popen` version) was deleted outright rather than
  kept around unused — its restart/no-restart-on-intentional-stop design directly
  informed this version, but once the target system stopped spawning raw `Popen`s,
  keeping the old class around would have been dead code.

### `TargetSystem.start()` fixed to wait for real health, not container state

- **Bug caught by test flakiness, not by inspection**: `start()` originally waited only
  for `docker inspect`'s `State.Running` to become `true` before returning, but a
  container can report `Running` before its Python process has finished binding its
  listening socket. The load balancer's very first proxied requests occasionally hit a
  replica in that gap, and urllib surfaces that specific failure mode as a raw
  `ConnectionResetError`, not a clean "connection refused" — confusing to read from a
  test traceback without already knowing the cause. Reproduced twice via
  `test_load_balancer_proxies_work_requests_to_a_backend` /
  `test_load_balancer_round_robins_across_all_replicas` failing intermittently.
  **Fix**: `start()` now polls each component's real `/health` endpoint until it
  answers 200 (with a timeout), not just the container's lifecycle state, before
  returning — the target system is only considered "up" once every component can
  actually answer traffic, which is the definition that actually matters to every
  caller downstream (probes, injectors, trial runner).

### `ProcessKillInjector` updated

- Now shells out to `docker kill --signal=SIGKILL <container_name>` instead of
  `os.kill(pid, SIGKILL)` on a cached PID — simpler and immune to the PID-namespace
  question entirely, since `docker kill` addresses the container by name regardless of
  its internal process tree.

### Tests updated for the Docker-based target system

- All of Day 1/2's test files (`test_target_system.py`, `test_probe.py`,
  `test_process_kill_injector.py`) updated to start/stop the real Docker Compose stack
  (via class-level `setUpClass`/`tearDownClass`, since container start/stop is much
  slower than spawning a bare Python process — a per-test fixture would have made the
  suite unacceptably slow) and to kill containers via `docker kill` instead of
  `Popen.kill()`.
- `test_supervisor.py` replaced by `test_container_supervisor.py` (2 cases): a killed
  container gets a new PID and `restart_count` increments correctly now that Docker's
  competing native policy is disabled; a restarted container answers `/health` again.
- Full suite: **21 tests, 0 failures** (`sg docker -c "python3 -m unittest discover -s
  tests"` — the `sg docker` wrapper is this tool session's own artifact, see above, not
  part of the committed test invocation for a normal terminal).

### Commits (chronological, continued)

5. `Rebuild target system on Docker containers, backends addressed by host:port`
6. `Container-based supervisor and process-kill injector; fix health-wait race in start()`

### Not yet built (per the 7-day plan, still ahead)

- Day 3: network-impairment injector (netns + netem) — root access confirmed working
  (`sudo -n ip`/`sudo -n tc`); building next.
- Day 4: resource-pressure injector via real (delegated, non-root) cgroup v2 limits, or
  `stress-ng` run inside a target container via `docker exec`.
- Day 5: full experiment batches (≥20 trials per fault type, including the LB-availability
  gap noted on Day 2); results database (already built, will just accumulate more rows).
- Day 6 (out of scope for this pass): statistical report generator, re-run on a real VPS.
- Day 7 (out of scope for this pass): README, architecture diagram, blog post, CV bullets.

---

## 2026-09-19 — Day 3: Network-impairment injector (netns + netem / hard partition)

### Reaching the container's netns without a container-runtime-specific API

Docker gives every container its own network namespace automatically, but doesn't
register it under `/run/netns` the way `ip netns add` does, so plain `ip netns exec
<name>` can't address it by name out of the box. Worked out (and verified directly,
before writing any injector code) that `ip netns attach <label> <pid>` bind-mounts an
*existing* process's netns (here, the container's PID-1, from `docker inspect
.State.Pid`) under `/run/netns/<label>` — after that, `ip netns exec <label> ...` and
`tc ... ` work against it exactly like any named namespace. This matters because it
means the injector needs privilege for exactly `ip`/`tc` and nothing else (no
`nsenter`, no manual `mkdir`/`ln` on `/run/netns`) — i.e., it fits inside the sudoers
scope already granted (`NOPASSWD: /usr/sbin/ip, /usr/sbin/tc`) with no changes needed.
- A child command run via `sudo -n ip netns exec <label> tc ...` does **not** need its
  own separate sudo grant for `tc`, even though `tc` is a different binary: `ip netns
  exec` itself runs as root (because sudo already elevated the outer `ip` invocation),
  and it simply `execve()`s its argument inside the target namespace, inheriting that
  root privilege directly — confirmed by testing it against the sudoers file exactly
  as configured, not assumed.

### Two distinct fault types, one injector class (`harness/injectors/network_impairment.py`)

Per the spec's own separation of "induced network partition" from "injected latency
(not full partition)" as different failure scenarios:

- **`mode="partition"`** — `ip link set eth0 down` inside the attached netns. A hard,
  total, both-directions loss of connectivity — chosen over `tc netem loss 100%`
  specifically because a link-down is unambiguous (verified: outbound SYN packets
  don't even leave the interface) where 100% netem loss can still behave subtly
  differently depending on retransmit/ARP behavior. Records as fault_type
  `"network-partition"`.
- **`mode="latency"`** — `tc qdisc add dev eth0 root netem delay <ms> [loss <pct>]`.
  Degraded, not unreachable. Records as fault_type `"network-latency"`.
- Both share one `NetworkImpairmentInjector` class (mode fixed at construction, not
  per-call) rather than two separate classes, since the netns-attach/detach plumbing
  is identical either way and only the actual `tc`/`ip link` command differs.
- **Self-healing via a `threading.Timer`**: unlike process-kill (where the supervisor
  provides recovery automatically) or a real production system (where an operator or
  automation would eventually fix a partition), nothing else in this harness would
  ever remove a network fault once applied. `inject()` schedules its own `heal()` via
  a timer after `duration_s`, so every network trial has a bounded, guaranteed
  recovery point — the same "never hang forever" discipline as Day 1's probe
  `wait_for` and Day 2's trial timeouts, now applied to the injector itself.
- `heal()`/`heal_all()` are idempotent and safe to call from test cleanup regardless
  of whether the timer already fired, specifically so a failing test can't leak a
  netns label or a permanently-down interface into the next test.

### Tests (`test_network_impairment_injector.py`, 3 cases, against the real Docker target system)

- **Partition detected and self-heals**: injects a 1s partition against `replica-1`,
  confirms the probe sees it go unhealthy, then confirms it comes back healthy no
  earlier than `injected_at + duration_s` (proves the recovery is actually gated by
  the timer, not some coincidental unrelated healthy blip).
- **Full trial via `TrialRunner`**: proves `NetworkImpairmentInjector` works through
  the exact same generic trial-running path Day 2 built for process-kill, with zero
  changes to `TrialRunner` itself — the `FaultInjector` interface abstraction from
  Day 2 paid for itself immediately here.
- **Latency injection measurably slows requests without an outage**: measures raw
  request round-trip time before/during/after a 250ms netem delay directly (not
  through the binary health probe), proving the netem effect is real and reversible,
  and specifically *not* relying on the probe's healthy/unhealthy signal for this one
  — see the false-negative-rate discussion below for why that distinction matters.
- Verified no leaked network namespaces after the full suite run (`sudo ip netns list`
  → empty).
- Full suite: **24 tests, 0 failures**.

### First small batch (informal, 8 process-partition trials against `replica-1`) — a direct answer to one of the spec's own difficult-follow-ups

```
network-partition: n=8 detected=8 recovered=8
  detection_ms: median=355.30 min=335.60 max=357.42
  recovery_ms:  median=1342.59 min=1324.44 max=1346.96
```

- **Detection latency here (~355ms) is roughly 15-18x process-kill's (~20ms), and
  this is a mechanism difference, not a "network faults are slower" finding.** A
  killed process causes an immediate TCP RST/connection-refused, which `urlopen`
  surfaces instantly. A link-down partition causes silent packet drops with no RST
  and no ICMP-unreachable back to the client, so the client's own connect *timeout*
  (the health probe's `timeout=0.3s`) has to fully elapse before the probe can call it
  "unhealthy." This is a direct, concrete answer to the spec's own difficult
  follow-up ("how do you know your health probe polling interval itself isn't
  dominating your measured detection latency?") — for this fault type specifically,
  the probe's *timeout* setting (not its poll interval) dominates the number almost
  entirely: ~300ms of the ~355ms median is that timeout, not anything about the fault
  itself. A citable Day 5/6 result will need to either report detection latency
  net of the known timeout floor, or use a shorter probe timeout for network faults
  specifically and say so explicitly.
- **Recovery time (~1343ms) is `duration_s` (1000ms, the injector's own healing
  timer) + detection latency (~355ms) + one more poll interval** — i.e., it is
  measuring exactly what was configured, not discovering something about the target
  system's resilience. This is expected and correctly attributable, unlike
  process-kill's recovery time (which really did measure something about restart
  speed) — worth being explicit about the difference when these numbers are
  eventually reported side by side in the Day 6 report.

### Known gap, carried forward from Day 2 and now sharper

The latency-injection test above deliberately bypassed the binary health probe and
measured raw request timing directly, specifically *because* a 250ms netem delay
would very likely make the probe's own 0.3s timeout flip it to "unhealthy" even
though the component is not actually down — a live demonstration of the exact
false-negative/degraded-vs-unhealthy gap flagged back on Day 1. Still not fixed (out
of scope for Day 3), but now there's a concrete, reproducible example of it rather
than just a theoretical concern, which will make it a stronger, better-evidenced
discussion point for the eventual blog post and the "what's actually different
between 'unhealthy' and 'degraded' in your probe?" interview question.

### Commits (chronological, continued)

7. `Network-impairment injector: netns-attached tc netem and hard link-down partition`

### Not yet built (per the 7-day plan, still ahead)

- Day 4: resource-pressure injector via real (delegated, non-root) cgroup v2 limits, or
  `stress-ng` run inside a target container via `docker exec`.
- Day 5: full experiment batches (≥20 trials per fault type, including the LB-availability
  gap from Day 2 and the probe-timeout-vs-detection-latency distinction from today);
  results database (already built, will just accumulate more rows).
- Day 6 (out of scope for this pass): statistical report generator, re-run on a real VPS.
- Day 7 (out of scope for this pass): README, architecture diagram, blog post, CV bullets.

---

## 2026-09-20 — Day 4: Resource-pressure injector (real cgroup limits + stress-ng) + scheduler

### Commit author email correction (unrelated to today's build, done first)

Before starting today's work, the user asked for every commit in this repo to use
`chovatiajaimin@gmail.com` instead of the `devxlabs.ai` address that had been used so
far. Rewrote history with `git filter-branch --env-filter` (author+committer email
only, every date/message/hash-otherwise preserved) across all 7 commits that existed
at that point, after stashing the in-progress Day 4 work first so the rewrite had a
clean tree to operate on. Verified afterward: `git log --format='%h %ae'` shows the
corrected address on every commit.

### Superseding the Day 1 cgroup-delegation finding

Day 1's environment check found that cgroup v2 controllers were delegated to the
user's own systemd slice, and reasoned that this would let resource pressure be
applied without root. That reasoning is now moot in a different way than expected:
since the target system runs as Docker containers (managed by `dockerd` as root, in
`system.slice/docker-*.scope`, not the user's delegated slice), the actual mechanism
used today is Docker's own resource limits (`cpus`/`mem_limit` in
`docker-compose.yml`), which dockerd translates into real cgroup v2 `cpu.max`/
`memory.max` settings on the container's cgroup. The delegated user-slice cgroups from
Day 1 are simply not involved anymore — noted here so the two findings aren't
confused with each other later.

### `stress-ng` installed *inside* the target image, not just on the host

`target-system/Dockerfile` now installs `stress-ng` in the image itself (`apt-get
install`), so the injector can run it via `docker exec -d <container> stress-ng ...`
directly inside the target container's own PID/cgroup namespace — contending against
that specific container's real resource limits, not the host's.

### Real resource limits added to the target containers

`docker-compose.yml`: every replica and the KV store now declare `cpus: "0.5"` and
`mem_limit: "128m"`. Verified these translate into real `HostConfig` values
(`docker inspect` → `NanoCpus=500000000`, `Memory=134217728`) rather than trusting the
YAML key names alone. The load balancer is deliberately left unconstrained — it's
harness infrastructure, never itself a fault target.

### `ResourcePressureInjector` (`harness/injectors/resource_pressure.py`)

- **`kind="cpu"`**: `docker exec -d <container> stress-ng --cpu <workers>
  --cpu-method all --timeout <duration_s>s`. Verified with `docker stats
  --no-stream`: a 2-worker stress run against a container capped at `cpus: "0.5"`
  drove measured CPU usage to ~49.7% (right at the 0.5-CPU ceiling), confirming the
  limit is real and the stress genuinely contends against it rather than the
  unconstrained host.
- **`kind="memory"`**: `stress-ng --vm 1 --vm-bytes <bytes> --vm-keep --timeout
  <duration_s>s`. `--vm-keep` holds allocated pages resident instead of
  freeing/reallocating every iteration, so it measures sustained pressure against
  `mem_limit`, not allocation churn.
- **No explicit `heal()`**: unlike the network injector, `stress-ng --timeout` stops
  itself, and `docker exec -d` returns immediately (detached) — the harness doesn't
  need to track or cancel this fault at all, which is the simplest of the three
  injectors built so far.
- `is_running()` uses `docker top <container>` (host-side process listing), not
  `docker exec ... pgrep` — caught immediately by manual testing: `pgrep` isn't
  present in the minimal `python:3.12-slim` image and installing `procps` just for a
  liveness check would have been unnecessary image bloat for something `docker top`
  already answers from the host side for free.

### Discovery: over-limit memory pressure OOM-kills the stress process, not the target — a real false-negative scenario, not just a hypothetical one

Manually tested `--vm-bytes 150M` against a container capped at `mem_limit: "128m"`
(above the limit, deliberately, before settling on a safe default): `docker stats`
showed memory pinned at 128MiB/128MiB (99.99%) with heavy block I/O (thrashing), yet
`docker inspect` reported `OOMKilled: false` for the *container*, and the target API
process kept answering `/health` normally throughout. The cgroup OOM killer reaped the
`stress-ng` child process specifically, not the container's PID-1 — so from the
outside, a component that was, for a moment, genuinely thrashing at its memory limit
looks indistinguishable from a perfectly healthy one. This is a live, reproduced
instance of the false-negative-rate concern raised on Day 1 and sharpened on Day 3,
not a theoretical one — good material for the eventual blog post and for the "what's
actually different between 'unhealthy' and 'degraded' in your probe?" interview
question. Not built further into the injector this pass (would need e.g. watching
`docker events` for `oom` actions to detect it, which is Day 5/6-report territory at
earliest) — the default `vm_bytes` was set safely under the limit (`100M` vs. a 128MB
cap) specifically so this doesn't happen nondeterministically inside the committed
test suite.

### Tests (`test_resource_pressure_injector.py`, 3 cases, against the real Docker target system)

- CPU pressure is real (measured via `docker stats`, not just "the command didn't
  error") and self-terminates within its `--timeout` window.
- Health probe stays healthy throughout moderate (2-worker, 1.5s) CPU pressure —
  the *lack* of a false negative at this stress level, a useful contrast point
  against the memory-OOM finding above, which the report will eventually want to
  discuss side by side.
- Memory pressure kept under the container's `mem_limit` does not trigger
  `OOMKilled` — confirms the safe default actually stays safe.

### `ExperimentScheduler` (`harness/scheduler.py`) — the last MUST-BUILD-adjacent piece before Day 5

- A thin plan runner: `add(injector, target_component, n_trials, **inject_kwargs)`
  queues an experiment; `run_all()` runs every queued experiment's full batch
  sequentially (deliberately not concurrently — concurrent faults against the same
  target system would contaminate each other's recovery measurements, which the
  spec's experimental-design section explicitly warns against) and returns every
  batch's records keyed by `(fault_type, target_component)`.
- Reuses Day 2's `TrialRunner` completely unchanged — this is the second injector
  (after `NetworkImpairmentInjector` on Day 3) to plug into the existing
  `FaultInjector`/`TrialRunner` abstraction with zero modifications to either,
  confirming that abstraction was worth building on Day 2.
- Tested with a small, fast plan (`test_scheduler.py`, 1 case, 4 total trials across
  two components) — Day 5's actual plan reuses this exact class unchanged, just
  queuing more experiments with bigger `n_trials`.

### Tests, full suite

**27 tests, 0 failures** (`sg docker -c "python3 -m unittest discover -s tests"`).

### Commits (chronological, continued)

8. `Resource-pressure injector: real stress-ng under cgroup cpu/memory limits, scheduler`

### Not yet built (per the 7-day plan, still ahead)

- Day 5: full experiment batches (≥20 trials per fault type — process-kill,
  network-partition, network-latency, cpu-pressure, memory-pressure — across the
  relevant target components) via `ExperimentScheduler`; results persisted to the
  SQLite recorder (already built).
- Day 6 (out of scope for this pass): statistical report generator, re-run on a real VPS.
- Day 7 (out of scope for this pass): README, architecture diagram, blog post, CV bullets.

---
