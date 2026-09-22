# Setup

The harness has two functional tiers, and they need different amounts of host
privilege:

| Tier | What it needs | Injectors it unlocks |
|---|---|---|
| **Docker-only** | Docker + Docker Compose | process-kill, resource-pressure |
| **Docker + sudo scope** | the above, plus a narrow `sudoers.d` rule for `ip`/`tc` | + network-impairment |

Start with the Docker-only tier to get the target system and most of the
harness running, then add the sudo scope when you're ready to exercise
`NetworkImpairmentInjector`. Skipping the sudo step is fine — everything else
still works, and `test_network_impairment_injector.py` is the only test file
that will fail without it.

## 1. Prerequisites

- **Docker Engine + Docker Compose v2** (the `docker compose` subcommand, not
  the standalone `docker-compose` binary). Verify with:

  ```bash
  docker compose version
  ```

- **Python 3.12+** — the harness and its tests run on the host, only the
  target system runs inside containers. No `pip install` step: the whole repo
  is stdlib-only by design (see [ARCHITECTURE.md](ARCHITECTURE.md#why-stdlib-only)),
  so there is no `requirements.txt` and no virtualenv to create.

- **Linux with cgroup v2 delegated to your user**, if you want
  `ResourcePressureInjector` to mean anything. This is normal on most modern
  distros; nothing extra to install for it — the actual CPU/memory limits are
  set per-container in `docker-compose.yml` (`cpus`, `mem_limit`), not on the
  host.

## 2. Bring up the target system

```bash
cd target-system
docker compose up -d --build
```

This builds one shared image (`target-system/Dockerfile`, based on
`python:3.12-slim` with `stress-ng` installed) and starts five containers:
`replica-1`, `replica-2`, `replica-3`, `kv-store`, `load-balancer`. Default
host ports: `9000` (load balancer), `9001`-`9003` (replicas, mostly useful for
debugging), `9010` (KV store) — override any of them via the
`REPLICA1_PORT` / `REPLICA2_PORT` / `REPLICA3_PORT` / `KV_PORT` / `LB_PORT`
environment variables before running `docker compose up`.

Confirm it's healthy:

```bash
curl http://localhost:9000/work    # routed through the load balancer
curl http://localhost:9010/health  # kv-store directly
```

Tear it down with `docker compose down` when you're done. Individual harness
components (`harness/launcher.py`'s `TargetSystem`) also start and stop these
containers programmatically — you don't need to run `docker compose` by hand
before running the test suite or `run-experiments.sh`; they manage the
containers' lifecycle themselves via `docker` CLI calls.

## 3. Run the test suite (Docker-only tier)

```bash
python3 -m unittest discover -s tests
```

Every test in this repo runs against the *real* target system — real
subprocesses, real containers, real sockets, real `SIGKILL`s. There is no
mocking anywhere in the suite. That means:

- The full suite takes a couple of minutes (each test starts and tears down
  real containers).
- `test_network_impairment_injector.py` will fail here until you complete
  step 4 below — that's expected, not a sign anything else is broken.
- Timing-sensitive assertions (e.g. "a restarted container answers `/health`
  again within N ms") can occasionally flake under host load, since they're
  measuring wall-clock behavior of a real container runtime rather than
  asserting against a mock. A one-off failure on a supervisor/timing test is
  worth re-running once before treating it as a real regression.

## 4. Grant the network-impairment injector's sudo scope

`NetworkImpairmentInjector` (see
[ARCHITECTURE.md](ARCHITECTURE.md#network-impairment-injector)) needs to run
`ip netns attach`, `ip link set ... down`, and `tc qdisc ... netem` as root
inside a target container's network namespace. Rather than asking for broad
Docker-equivalent root, the harness is scoped to exactly two binaries:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: /usr/sbin/ip, /usr/sbin/tc" \
  | sudo tee /etc/sudoers.d/chaos-harness-netns
sudo chmod 440 /etc/sudoers.d/chaos-harness-netns
```

This lets your user run `sudo -n ip ...` and `sudo -n tc ...` without a
password prompt, and nothing else. Verify it took effect:

```bash
sudo -n ip netns list   # should succeed with no password prompt
```

Why this specific shape, not a broader grant:

- **Scoped to two binaries, not `ALL`** — the injector's own code
  (`harness/injectors/network_impairment.py`) only ever shells out to `ip` and
  `tc`; there's no reason to grant more than that.
- **`ip netns attach <label> <pid>` reaches the container's namespace directly**
  by bind-mounting the target container's existing `/proc/<pid>/ns/net` under
  `/run/netns/<label>` — this needs no separate `mkdir`/`ln` privilege and no
  container-runtime-specific API (works identically regardless of whether the
  target is Docker, Podman, or a bare process), because `ip netns attach`
  handles the bind-mount internally.
- **One `sudo` call per invocation, not one for `ip netns exec` and another for
  the child command** — `ip netns exec <label> tc ...` runs as root because
  `ip netns exec` itself runs as root under the sudo grant above and execs its
  argument inside that namespace, inheriting the privilege. No second sudo
  prompt or grant is needed for the nested `tc`/`ip` call.

With this in place, re-run the full suite — `test_network_impairment_injector.py`
should now pass:

```bash
python3 -m unittest discover -s tests
```

## 5. Run the full chaos experiment batch

```bash
./run-experiments.sh
```

This runs `harness/run_experiments.py`: ≥20 trials for every fault type
(process-kill, network-partition, network-latency, cpu-pressure,
memory-pressure) against the real Docker-based target system, and persists
every trial to `results/results.db` (SQLite, gitignored — regenerate it
on-demand rather than committing it; see
[ARCHITECTURE.md](ARCHITECTURE.md#results-database)). This is the same
reproducibility path used to produce the findings summarized in the main
[README](../README.md#findings).

Inspect the results directly:

```bash
sqlite3 results/results.db "SELECT fault_type, target_component, COUNT(*), \
  SUM(detected), SUM(recovered) FROM trials GROUP BY fault_type, target_component;"
```

## Troubleshooting

- **`docker: permission denied`** — your user isn't in the `docker` group, or
  the Docker daemon isn't running. This project does not attempt to work
  around that; add yourself to the group (`sudo usermod -aG docker $USER`,
  then re-login) or start the daemon.
- **`sudo: a password is required` from the network-impairment injector** —
  step 4 wasn't completed, or the sudoers rule path (`/usr/sbin/ip`,
  `/usr/sbin/tc`) doesn't match your distro's actual binary paths. Check with
  `which ip tc` and adjust the sudoers rule to match.
- **cpu-pressure / memory-pressure trials never show `detected=1`** — this is
  very likely not a bug. Read
  [README.md's Findings section](../README.md#findings) first: the current
  binary health probe genuinely cannot see this class of fault, and that's
  the project's headline result, not an error state.
