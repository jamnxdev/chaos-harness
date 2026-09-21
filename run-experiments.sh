#!/bin/bash
# Runs the full Day 5 chaos experiment batch (>=20 trials per fault type) against the
# real Docker-based target system, persisting results to results/results.db. Requires
# Docker and the passwordless-sudo scope for `ip`/`tc` set up during the Docker
# migration (see local-doc/IMPLEMENTATION_LOG.md).
set -euo pipefail
cd "$(dirname "$0")"
python3 harness/run_experiments.py
