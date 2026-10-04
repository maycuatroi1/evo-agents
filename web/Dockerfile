# syntax=docker/dockerfile:1.7
# The hub's web image: the Next.js standalone server. Build it from web/:
#
#   docker build -t evo-agents-hub-web:local web
#
# EVO_HUB_API_INTERNAL_URL is read twice. At run time it is where server components reach the API (required). At
# build time, where next.config.ts fixes the rewrite, the build forwards /v1/* to it, so the browser gets the API on
# the web's own origin even without a reverse proxy in front. The build argument defaults to http://api:8080, the
# api service of deploy/hub/docker-compose.yml; pass --build-arg EVO_HUB_API_INTERNAL_URL=<url> for another
# address, or an empty value for an image without the rewrite. In production the reverse proxy sends /v1 and /mcp
# to the API before a request reaches this server, so the rewrite only serves what reaches the web by mistake.
# The base image is pinned by tag and digest; bump both together.

ARG NODE_IMAGE=node:22.23.3-trixie-slim@sha256:b26b04c123d9ff8ab646ceb18b9d75a1173acf64b9a401094b906d27b29338d4

FROM ${NODE_IMAGE} AS deps
ENV PNPM_HOME=/pnpm \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable
WORKDIR /web
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

FROM deps AS build
# Fixed into the build: see the top of this file.
ARG EVO_HUB_API_INTERNAL_URL=http://api:8080
ENV NEXT_TELEMETRY_DISABLED=1 \
    EVO_HUB_API_INTERNAL_URL=${EVO_HUB_API_INTERNAL_URL}
COPY . .
# public/ is optional in the source; the runtime stage copies it either way.
RUN mkdir -p public && pnpm build

FROM ${NODE_IMAGE}
LABEL org.opencontainers.image.title="evo-agents-hub-web" \
      org.opencontainers.image.description="evo-agents hub: web interface (Next.js standalone)" \
      org.opencontainers.image.source="https://github.com/maycuatroi1/evo-agents" \
      org.opencontainers.image.licenses="Apache-2.0"
# The same address is the run-time default, so the image reaches the api of the compose file as it is; the
# environment overrides it (deploy/hub/docker-compose.yml sets it).
ARG EVO_HUB_API_INTERNAL_URL=http://api:8080
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    EVO_HUB_API_INTERNAL_URL=${EVO_HUB_API_INTERNAL_URL}
WORKDIR /app
# `next build` leaves public/ and .next/static outside .next/standalone; the server needs them beside server.js.
COPY --from=build --chown=node:node /web/.next/standalone ./
COPY --from=build --chown=node:node /web/.next/static ./.next/static
COPY --from=build --chown=node:node /web/public ./public
USER node
EXPOSE 3000
# A static file: the server answers it without asking the API, so an API outage does not mark the web unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/icon.svg').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "server.js"]
