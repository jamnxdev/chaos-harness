# Single shared image for every target-system component (stdlib-only, so there is
# nothing to install beyond the base image) -- docker-compose.yml differentiates
# replica/kv-store/load-balancer purely via the `command` each service overrides.
FROM python:3.12-slim
WORKDIR /app
COPY api_replica.py kv_store.py load_balancer.py ./
EXPOSE 8080
