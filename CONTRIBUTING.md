# Contributing

Chaos Harness is a single-author portfolio project, but it's built and
documented like a normal open-source repo, and contributions (fixes,
new injectors, better experimental design, doc corrections) are genuinely
welcome. This file is the practical guide to working in the codebase; for how
the pieces fit together, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
first.

## Before you start

1. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — especially
   [Design decisions](docs/ARCHITECTURE.md#design-decisions) and
   [Findings](docs/ARCHITECTURE.md#findings). A lot of design choices here
   (stdlib-only, sequential-not-concurrent experiments, a deliberately binary
   health probe) look like gaps until you've read why they're there.
2. Follow [docs/SETUP.md](docs/SETUP.md) to get Docker, the target system, and
   (optionally) the network-impairment sudo scope working locally.
3. Run the full test suite once before changing anything, so you have a known
   baseline:

   ```bash
   python3 -m unittest discover -s tests
   ```

## Ground rules

- **No third-party dependencies.** The project is stdlib-only by design (see
  [ARCHITECTURE.md](docs/ARCHITECTURE.md#why-stdlib-only)). If a change seems
  to need a package, look for a stdlib way to do it first, and if there
  genuinely isn't one, open an issue to discuss it before sending a PR — this
  is a deliberate constraint, not an oversight.
- **No simulated faults.** Every injector applies a real fault (a real
  signal, a real `tc netem` rule, a real `stress-ng` process) and measures the
  real result. A PR that introduces a `sleep()`-based stand-in for an actual
  fault will be rejected on principle — it undermines the one thing this
  project is for.
- **Every fault-injection code path needs a test that runs against the real
  target system.** There is no mocking anywhere in `tests/` — a test that
  starts real containers/processes and asserts on real behavior. Match that
  style; a test that only asserts a class instantiates without touching the
  real target system isn't testing the thing that matters here.
- **Timeouts, everywhere a wait happens.** Any new polling loop needs a hard
  deadline (see `HealthProbe.wait_for` and `TrialRunner`'s use of it). A fault
  that never resolves must never hang the harness — this is called out
  explicitly in the architecture doc as a hard requirement, not a nice-to-have.
- **Document deviations, don't silently patch around them.** If you hit an
  environment constraint or a platform behavior that doesn't work as expected
  (the way `restart: unless-stopped` turned out not to fire reliably — see
  [ARCHITECTURE.md](docs/ARCHITECTURE.md#why-containersupervisor-doesnt-trust-restart-unless-stopped)),
  say so in the PR description rather than quietly working around it. Future
  readers should be able to tell the difference between "this is how it
  works" and "this is a workaround for X."

## Adding a new fault injector

Every injector implements the same tiny interface
(`harness/injectors/base.py`):

```python
from harness.injectors.base import FaultInjector, InjectionResult

class MyInjector(FaultInjector):
    fault_type = "my-fault"

    def inject(self, target_component: str, **kwargs) -> InjectionResult:
        injected_at = self.now()
        # ... apply the fault for real, no simulation ...
        return InjectionResult(
            fault_type=self.fault_type,
            target_component=target_component,
            injected_at=injected_at,
        )
```

Checklist for a new injector:

1. `inject()` must apply the fault and return immediately — it does not wait
   for or manage recovery. `TrialRunner` handles detection/recovery timing via
   `HealthProbe`; keep that separation.
2. If your fault needs an explicit cleanup step (unlike
   `ResourcePressureInjector`'s self-timing `stress-ng`, or
   `ProcessKillInjector`'s implicit recovery via `ContainerSupervisor`), add a
   `heal(target_component)` method, following
   `NetworkImpairmentInjector`'s pattern.
3. Add a test file under `tests/` that runs the injector against the real
   target system started via `harness.launcher.TargetSystem`, following the
   existing `test_*_injector.py` files as a template: at minimum, one test
   that a single `inject()` call is detected and recovered by a full
   `TrialRunner` trial.
4. Wire it into `harness/run_experiments.py` if it should be part of the
   standard reproducible batch, and note the target-component choice
   rationale in a comment if it's not obvious (see that file's existing
   comments on why each fault type targets the component it does).
5. Update [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)'s fault-injector
   section with what the fault does and any host-privilege requirements it
   needs, and add setup steps to [docs/SETUP.md](docs/SETUP.md) if it needs
   anything beyond Docker.

## Running tests

```bash
python3 -m unittest discover -s tests          # full suite
python3 -m unittest tests.test_process_kill_injector   # one file
```

A few things worth knowing before you file a bug against a failing test:

- The full suite takes a couple of minutes — it's starting and tearing down
  real containers, not asserting against mocks.
- `test_network_impairment_injector.py` requires the sudo scope from
  [SETUP.md](docs/SETUP.md#4-grant-the-network-impairment-injectors-sudo-scope).
  Failing without it is expected.
- Timing-sensitive assertions (container-restart latency, probe transition
  timing) measure real wall-clock behavior and can occasionally flake under
  host load. Re-run once before assuming it's a real regression; if it's
  consistently flaky, that's worth its own issue/PR rather than a silent
  retry-until-green loop.

## Commit and PR conventions

- Commit messages: short imperative summary line (`Add memory-pressure
  cooldown to resource_pressure injector`, not `Added` / `Adding`), body only
  if the *why* isn't obvious from the diff.
- Keep PRs scoped to one logical change. A new injector, a probe fix, and a
  doc update are three PRs, not one.
- If your change affects any of the numbers in
  [docs/ARCHITECTURE.md#findings](docs/ARCHITECTURE.md#findings) (e.g. a probe
  change that would affect detection latency), re-run
  `./run-experiments.sh` and note the before/after in the PR description —
  this project's credibility rests on its numbers actually reflecting the
  current code, not a stale run.

## Reporting issues

Bug reports and design discussions are both welcome as GitHub issues. For a
bug, include: which fault type/component, whether it reproduces against a
fresh `docker compose up`, and the relevant rows from `results/results.db` or
test output if applicable. For a design question (e.g. "should the probe
support degraded-vs-down health?"), a reference to the relevant
[Findings](docs/ARCHITECTURE.md#findings) entry helps ground the discussion.
