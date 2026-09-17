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
