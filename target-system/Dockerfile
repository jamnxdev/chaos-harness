# Shared image for every target-system component (the Python scripts themselves are
# still stdlib-only) -- docker-compose.yml differentiates replica/kv-store/
# load-balancer purely via the `command` each service overrides.
#
# stress-ng is installed here (not just on the host) so the resource-pressure
# injector can run it *inside* the target container via `docker exec`, subject to
# that specific container's own cgroup cpu/memory limits (set in docker-compose.yml)
# -- real cgroup-enforced pressure, not a simulation.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends stress-ng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY api_replica.py kv_store.py load_balancer.py ./
EXPOSE 8080
