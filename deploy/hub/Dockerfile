# syntax=docker/dockerfile:1.7
# The hub's Python image: `evo-agents hub serve` (the api, the default command) and `evo-agents hub worker` (the
# worker, same image, its own command in docker-compose.yml). Build it from the repository root:
#
#   docker build -f deploy/hub/Dockerfile -t evo-agents-hub:local .
#
# Only pyproject.toml, README.md, LICENSE, MANIFEST.in and evo_agents/ reach the build (Dockerfile.dockerignore).
# The base image is pinned by tag and digest; bump both together, in both stages.

ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY evo_agents ./evo_agents
# A wheel, not an editable install: the runtime stage gets the virtualenv and nothing of /src.
RUN /opt/venv/bin/pip install '.[hub-server,graphify]' \
    && /opt/venv/bin/evo-agents --version

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="evo-agents-hub" \
      org.opencontainers.image.description="evo-agents hub: API server and background worker" \
      org.opencontainers.image.source="https://github.com/maycuatroi1/evo-agents" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVO_HUB_DATA_DIR=/cache
RUN groupadd --system --gid 10001 hub \
    && useradd --system --uid 10001 --gid hub --home-dir /home/hub --create-home --shell /usr/sbin/nologin hub \
    && install -d -o hub -g hub -m 0750 /cache
COPY --from=build /opt/venv /opt/venv
USER hub:hub
WORKDIR /home/hub
EXPOSE 8080
# Liveness only: /v1/health/live answers while the process serves requests. /v1/health also asks Postgres and R2,
# and an R2 outage must not get the api restarted while it still answers kg_* from its cache.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health/live', timeout=4)"]
CMD ["evo-agents", "hub", "serve", "--host", "0.0.0.0", "--port", "8080", "--data-dir", "/cache"]
