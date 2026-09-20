"""Runs a fixed plan of experiments (injector x target component x trial count)
back-to-back and unattended -- the piece that turns Day 2's single-trial `TrialRunner`
into something that can produce the spec's required >=20-trials-per-fault-type
batches (Day 5) without a human driving each call.
"""
from dataclasses import dataclass

from harness.trial_runner import TrialRunner


@dataclass
class Experiment:
    injector: object
    target_component: str
    n_trials: int
    inject_kwargs: dict


class ExperimentScheduler:
    def __init__(self, target_system, recorder, run_id: str, **trial_runner_kwargs):
        self.target_system = target_system
        self.recorder = recorder
        self.run_id = run_id
        self.trial_runner_kwargs = trial_runner_kwargs
        self.experiments: list[Experiment] = []

    def add(self, injector, target_component: str, n_trials: int, **inject_kwargs):
        self.experiments.append(Experiment(injector, target_component, n_trials, inject_kwargs))
        return self

    def run_all(self) -> dict:
        """Runs every added experiment's full batch in order, returning
        {(fault_type, target_component): [TrialRecord, ...]}. Experiments run
        sequentially, not concurrently -- concurrent faults against the same target
        system would contaminate each other's recovery measurements, which the
        spec's own experimental-design section explicitly warns against."""
        results = {}
        for experiment in self.experiments:
            runner = TrialRunner(
                self.target_system, experiment.injector, self.recorder, self.run_id,
                **self.trial_runner_kwargs,
            )
            records = runner.run_batch(experiment.target_component, experiment.n_trials, **experiment.inject_kwargs)
            results[(experiment.injector.fault_type, experiment.target_component)] = records
        return results
