# syntax=docker/dockerfile:1.7
# The hub's Python image: `evo-agents hub serve` (the api, the default command), `evo-agents hub worker` (the worker)
# and `evo-agents hub plugin-host` (the plugins service, as root with four capabilities, its volume at /plugins), same
# image, their own commands in docker-compose.yml. Build it from the repository root:
#
#   docker build -f deploy/hub/Dockerfile -t evo-agents-hub:local .
#
# Only pyproject.toml, README.md, LICENSE, MANIFEST.in, evo_agents/ and deploy/hub/core-plugins.txt reach the build
# (Dockerfile.dockerignore). The base image is pinned by tag and digest; bump both together, in both stages.
#
# Core plugins (docs/hub-deployment.md, section Core plugins in the image) go into the same virtualenv: those pinned
# in deploy/hub/core-plugins.txt, from PyPI, then a hub's own, the requirements of the build argument
# EVO_HUB_EXTRA_CORE_PLUGINS, from the index the optional build secret core_plugin_index names, else PyPI:
#
#   docker build -f deploy/hub/Dockerfile --build-arg EVO_HUB_EXTRA_CORE_PLUGINS="lumen-relay==1.0.0 --hash=sha256:..." \
#     --secret id=core_plugin_index,env=EVO_HUB_CORE_PLUGIN_INDEX_URL -t evo-agents-hub:local .

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
# pip installs exactly the lines evo_agents.plugins.core_install wrote (--no-deps), each wheel against its hash, and
# never builds a source distribution. -I keeps /src off the path: the helper is the installed one.
COPY deploy/hub/core-plugins.txt /tmp/core-plugins.txt
RUN /opt/venv/bin/python -I -m evo_agents.plugins.core_install pinned /tmp/core-plugins.txt > /tmp/pinned.txt \
    && if [ -s /tmp/pinned.txt ]; then \
         /opt/venv/bin/pip install --no-deps --require-hashes --only-binary=:all: \
           --index-url https://pypi.org/simple -r /tmp/pinned.txt; \
       fi
# The secret's URLs, which may hold a credential, live in its tmpfs and in the environment of the one pip command,
# never in a file of a layer; the argument's requirements are no secret (`docker history` of this stage shows them).
# pip check finds a dependency the requirements left out; check fails the build when a core plugin does not load.
ARG EVO_HUB_EXTRA_CORE_PLUGINS=""
RUN --mount=type=secret,id=core_plugin_index,required=false \
    set -e; \
    if [ -n "$EVO_HUB_EXTRA_CORE_PLUGINS" ]; then \
      /opt/venv/bin/python -I -m evo_agents.plugins.core_install extra /tmp/core-plugins.txt > /tmp/extra.txt; \
      /opt/venv/bin/python -I -m evo_agents.plugins.core_install index; \
      if [ -s /tmp/extra.txt ]; then \
        set -f; set -- $(cat /run/secrets/core_plugin_index 2>/dev/null || true); set +f; \
        first="${1:-https://pypi.org/simple}"; [ "$#" -eq 0 ] || shift; \
        PIP_INDEX_URL="$first" PIP_EXTRA_INDEX_URL="$*" \
          /opt/venv/bin/pip install --no-deps --require-hashes --only-binary=:all: -r /tmp/extra.txt; \
      fi; \
    fi; \
    /opt/venv/bin/pip check; \
    /opt/venv/bin/python -I -m evo_agents.plugins.core_install check

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
    && install -d -o hub -g hub -m 0750 /cache \
    && install -d -o root -g root -m 0755 /plugins
COPY --from=build /opt/venv /opt/venv
USER hub:hub
WORKDIR /home/hub
EXPOSE 8080
# Liveness only: /v1/health/live answers while the process serves requests. /v1/health also asks Postgres and R2,
# and an R2 outage must not get the api restarted while it still answers kg_* from its cache.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health/live', timeout=4)"]
CMD ["evo-agents", "hub", "serve", "--host", "0.0.0.0", "--port", "8080", "--data-dir", "/cache"]
