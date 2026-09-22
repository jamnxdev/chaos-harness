# Chaos Harness

A small chaos-engineering harness that injects **real** faults — process
kills, network partitions/latency, CPU/memory pressure — into a real,
disposable distributed system, and measures how it actually detects and
recovers. Every fault is real: a real `SIGKILL`, a real `tc netem` rule
inside a real network namespace, real `stress-ng` load fighting a real
cgroup limit. Nothing here is simulated, and that's the whole point — the
project's only claim to credibility is that its numbers describe what
actually happened.

The headline result: a straightforward binary HTTP health check — the kind
almost every small service ships with — detected **0 of 20** CPU/memory
pressure trials. Detailed below in [Findings](#findings).

## Why this exists

It's easy to assume a health check + auto-restart setup handles "the system
recovers from failures" as a solved problem. This project tests that
assumption directly, one fault type at a time, with a real reproducible batch
of trials behind every number, instead of taking it on faith.

## What it is

```
                      ┌───────────────────────────────────────────┐
                      │              target-system/                │
   client ──▶ load-balancer ──▶ replica-1 / replica-2 / replica-3   │
                      │         kv-store  (singleton, no LB)        │
                      └───────────────────────────────────────────┘
                                       ▲
                      inject fault     │  poll /health
                      ┌────────────────┴────────────────┐
                      │              harness/            │
                      │   injectors ──▶ TrialRunner ──▶  │
                      │                       ResultRecorder │
                      │                       results.db  │
                      └───────────────────────────────────┘
```

- **`target-system/`** — 3 stateless API replicas behind a round-robin load
  balancer, plus one deliberately-unreplicated stateful KV store. Runs as
  Docker containers.
- **`harness/`** — fault injectors (process-kill, network-impairment,
  resource-pressure), a health probe, a trial runner that ties fault →
  detection → recovery → results-database recording into one measured trial,
  and a scheduler that runs full experiment batches unattended.

Full component-by-component breakdown, data flow, and the reasoning behind
every non-obvious design choice: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quickstart

Requires Docker + Docker Compose v2 and Python 3.12+. No `pip install` —
the project is stdlib-only (see
[why](docs/ARCHITECTURE.md#why-stdlib-only)).

```bash
git clone git@github.com:jamnxdev/chaos-harness.git
cd chaos-harness

# bring up the target system
cd target-system && docker compose up -d --build && cd ..

# run the test suite (real containers, no mocking)
python3 -m unittest discover -s tests

# run the full reproducible experiment batch (>=20 trials per fault type)
./run-experiments.sh

# inspect the results
sqlite3 results/results.db "SELECT fault_type, target_component, COUNT(*), \
  SUM(detected), SUM(recovered) FROM trials GROUP BY fault_type, target_component;"
```

The network-impairment fault type (partition/latency) needs one extra
one-time setup step (a narrowly-scoped `sudoers.d` rule for `ip`/`tc`) —
everything else works without it. Full walkthrough, including that step and
troubleshooting: **[docs/SETUP.md](docs/SETUP.md)**.

## Findings

Full batch: ≥20 trials per fault type, 120 trials total, across 6
fault-type/component combinations. Reproducible via `./run-experiments.sh`.

```
fault_type           component      n  detected  recovered
process-kill         replica-1     20        20         20
process-kill         kv-store      20        20         20
network-partition    replica-2     20        20         20
network-latency      replica-2     20        20         20
cpu-pressure         replica-3     20         0          0
memory-pressure      replica-3     20         0          0
```

**1. The health probe cannot see resource pressure at all.** CPU and memory
pressure were detected in 0 of 20 trials each — not a fluke, a full batch
confirming what individual manual runs already showed. The `/health` handler
is cheap enough that it keeps answering fast and green even while the
container is genuinely, measurably constrained. At these stress levels, with
this probe design, **the false-negative rate for resource-pressure faults is
100%.**

**2. Containerizing the target system has a real, measured latency cost.**
The same process-kill fault type, measured on the original plain-process
target system vs. the Docker-based one: detection latency rose from ~20ms to
~234ms median (~10×), recovery from ~100ms to ~825-835ms median (~8×) — a
concrete, measured cost of Docker's port-publishing and container-restart
bookkeeping, not a defect.

**3. Partition and latency are currently indistinguishable.** A hard
link-down and a 250ms `netem` delay land on statistically identical
detection/recovery numbers (~355ms / ~1346ms), because both reliably cross
the probe's ~0.3s connect timeout. The experimental design can't currently
tell "fully down" apart from "badly degraded" using timing alone.

Full analysis, numbers, and reasoning for each finding:
[docs/ARCHITECTURE.md#findings](docs/ARCHITECTURE.md#findings).

## Documentation map

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component-by-component breakdown, data flow, every non-obvious design decision, full findings analysis |
| [docs/SETUP.md](docs/SETUP.md) | Full environment setup, including the network-impairment sudo scope walkthrough and troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev workflow, how to add a new fault injector, test conventions, commit/PR conventions |
| [local-doc/IMPLEMENTATION_LOG.md](local-doc/IMPLEMENTATION_LOG.md) | The running build log this project was actually developed against — day-by-day decisions, bugs caught, and why, in the order they happened |

## Project status

Built end-to-end: target system, three fault injectors, health probe, trial
runner, results database, and a scheduler capable of running full
reproducible batches. Not yet built: a statistical report/chart generator
(the raw data and the numbers above cover current analysis needs) and a
richer degraded-vs-down health signal (see
[Finding 1](docs/ARCHITECTURE.md#finding-1-the-health-probe-cannot-see-resource-pressure-at-all)
for why that's the natural next step). See
[docs/ARCHITECTURE.md#whats-intentionally-out-of-scope](docs/ARCHITECTURE.md#whats-intentionally-out-of-scope)
for the full list.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
dev workflow, how to add a new fault injector, and test/commit conventions.

## License

[MIT](LICENSE) © 2026 Jaimin Chovatia
