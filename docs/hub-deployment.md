# Deploying a hub for a client

A client of evo-agents runs a hub of its own, built from the same images as every other hub: its own database,
bucket, domain, GitHub sign-in and secrets, its own branding and features, and its own set of plugins. One hub serves
one client. The hub has no tenants; its projects keep the client's teams apart (`docs/hub.md`, section Projects and
who sees what). Nothing in the code changes for a client. What differs is set in three places: the operator's
variables in the platform's environment, the settings a hub admin makes in the hub, which its database keeps, and the
plugins, which a hub admin installs from the store or the operator builds into a child image.

`docs/hub.md`, section Deploying, is the reference for the images, the compose file and every variable. This page
puts them in order for a new instance.

## What an instance has of its own

| Part | Where it is set | Notes |
| --- | --- | --- |
| Postgres database | `EVO_HUB_DSN` | never shared between hubs: the schema, the plugin store, the settings and the audit trail belong to one hub |
| Blob bucket | `EVO_HUB_S3_ENDPOINT`, `EVO_HUB_S3_BUCKET` and its key | a bucket of its own, and a key limited to it |
| Domain | `EVO_HUB_PUBLIC_URL` and the reverse proxy | `/v1` and `/mcp` to the api, everything else to the web |
| GitHub OAuth App | `EVO_HUB_GITHUB_CLIENT_ID`, `EVO_HUB_GITHUB_CLIENT_SECRET` | device flow on, callback `EVO_HUB_PUBLIC_URL` + `/v1/auth/web/callback` |
| Secrets | `EVO_HUB_SESSION_SECRET`, `EVO_HUB_SECRETS_KEY`, `EVO_HUB_PLUGIN_HOST_TOKEN` | generated for this hub, never copied from another |
| Hub admins | `EVO_HUB_ADMINS` | the client's GitHub logins |
| Trusted publishers | `EVO_HUB_PLUGIN_PUBLISHERS`, `EVO_HUB_PLUGIN_CORE_PUBLISHERS` | whose signed bundles this hub's store accepts |
| Branding and features | the hub (Administration, Branding and Features) | kept in the database with versions; `docs/hub.md`, section Branding and features |
| Isolated plugins | the hub's store, then each project | signed bundles; their backends run in the `plugins` service |
| Core plugins | the image | a child image built FROM the hub's image (below) |

## The images

A client's hub runs a release: `EVO_HUB_VERSION=<version>` makes the compose file pull
`ghcr.io/maycuatroi1/evo-agents-hub:<version>` and `ghcr.io/maycuatroi1/evo-agents-hub-web:<version>`. The hub of
evo-agents itself runs main, built on its platform from the checkout; no image of main is published. A client's hub
stays on its release until the operator moves `EVO_HUB_VERSION` (`docs/hub.md`, section Operations: take a backup
first when the release has a migration). As it starts, the api turns off every installed plugin whose `engines` no
longer covers the release, and every hub admin who has signed in gets the notice `plugin_disabled`: run
`evo-agents hub plugin list` after an upgrade and upgrade those plugins.

## A child image

Most clients need none. Isolated plugins come through the store, and branding and features through the hub. Build a
child image only to add core plugins, which are Python packages that run in the hub's own processes, or files the
hub's image lacks, such as a company's CA certificate. It starts FROM the hub's image of one release and changes
nothing of evo-agents:

```dockerfile
# The hub image of one evo-agents release, with Acme's core plugin.
ARG EVO_HUB_VERSION
FROM ghcr.io/maycuatroi1/evo-agents-hub:${EVO_HUB_VERSION}
USER root
COPY wheels/ /tmp/wheels/
RUN /opt/venv/bin/pip install --no-cache-dir --no-index --find-links /tmp/wheels acme-hub-notices==1.4.0 \
    && rm -rf /tmp/wheels \
    && /opt/venv/bin/evo-agents --version
USER hub:hub
```

```sh
docker build --build-arg EVO_HUB_VERSION=<version> -t registry.acme.example/acme-hub:<version>-1 .
```

- The base image keeps its virtual environment at `/opt/venv` and runs as the user `hub`. Install into that
  environment and end on `USER hub:hub`, so the api and the worker run as before; the compose file starts the
  `plugins` service as root itself.
- A core plugin names its manifest in the entry point group `evo_agents.plugins` (`docs/plugins.md`, section Core
  plugins). Installing it is what trusts it: nothing signs it and the store does not check it, so install it from
  wheels you reviewed, pinned, without an index.
- A core plugin from outside evo-agents contributes channels, routes, MCP tools, jobs and events, but no `tables` and
  no native `pages`. The hub's tables come from its one chain of migrations, which is in evo-agents, and the hub
  records a core plugin only when every table the plugin owns exists. A native page is part of the web image's build.
  A plugin that needs either belongs in evo-agents.
- Pin the base by digest as well (`FROM ghcr.io/maycuatroi1/evo-agents-hub:<version>@sha256:...`) so that a rebuild
  takes the same bytes.

The child image replaces the hub's image for the api, the worker and the plugins service; the web image stays
evo-agents'. An override file next to the compose file names it, and drops the build so that compose pulls it:

```yaml
# docker-compose.acme.yml
services:
  api:
    image: registry.acme.example/acme-hub:${ACME_HUB_TAG:?}
    build: !reset null
  worker:
    image: registry.acme.example/acme-hub:${ACME_HUB_TAG:?}
    build: !reset null
  plugins:
    image: registry.acme.example/acme-hub:${ACME_HUB_TAG:?}
    build: !reset null
```

```sh
docker compose -f deploy/hub/docker-compose.yml -f docker-compose.acme.yml up -d   # never --build here
```

## The operator's variables

Start from `deploy/hub/.env.example` and set every value in the platform's environment, never in a committed file.

1. **The hub itself.** `EVO_HUB_VERSION`, `EVO_HUB_DSN`, `EVO_HUB_ADMINS`, the GitHub OAuth App's client id and
   secret, `EVO_HUB_SESSION_SECRET`, `EVO_HUB_PUBLIC_URL` and the four `EVO_HUB_S3_*` variables, which the branding's
   logo and favicon and the plugin store need too. `EVO_HUB_SECRETS_KEY` seals the credentials of runs and the secret
   fields of plugins' config; without it, setting either answers 503.
2. **The publishers it trusts.** `EVO_HUB_PLUGIN_PUBLISHERS` is a comma-separated list of `ID=sigstore:...` and
   `ID=ed25519:...` entries, several for one id if need be. A Sigstore entry names the GitHub Actions workflow that
   signs the publisher's bundles, `OWNER/REPO/.github/workflows/FILE.yml`; an Ed25519 entry names a public key, 32
   bytes in base64, for a hub that cannot reach Sigstore. Unset, the hub trusts nobody and refuses every bundle.
   `EVO_HUB_PLUGIN_CORE_PUBLISHERS` lists the ids among them that may publish core plugins.

   ```sh
   EVO_HUB_PLUGIN_PUBLISHERS=acme=sigstore:acme/hub-plugins/.github/workflows/release.yml,acme=ed25519:<public key>
   ```

3. **The plugin host.** `EVO_HUB_PLUGIN_HOST_TOKEN` is a secret the api, the worker and the `plugins` service share,
   at least 32 random characters (`python -c 'import secrets; print(secrets.token_urlsafe(32))'`); the hub refuses a
   value an example ever showed. `EVO_HUB_PLUGIN_HOST_URL` takes the value of `.env.example`,
   `http://evo-agents-hub-plugins:8090`, the service's alias on the stack's own network. `EVO_HUB_PLUGINS_CPUS`,
   `EVO_HUB_PLUGINS_MEMORY` and `EVO_HUB_PLUGINS_PIDS` cap every backend together (default `1`, `2g` and `512`).
   Without the token and the URL the hub runs, the store and core plugins work, and no isolated backend starts.
4. **What the client uses besides**: the GitHub App that gives runs their tokens, the Curator's App, Telegram and
   Sentry, each as `docs/hub.md` says.

The `plugins` service runs as root with only the capabilities SETUID, SETGID, CHOWN and KILL, so that each backend
gets a user of its own (`docs/plugins.md`, section Where a backend runs). A platform that allows neither root nor
added capabilities still runs the hub, but the host then refuses to start any backend, and isolated plugins stay
down; `--allow-shared-user`, which lifts that, is for a developer's machine only.

## Starting and setting it up

1. Start the stack and check `GET /v1/health`: `status` ok, `db` ok, `r2` ok, and the schema of the release.
2. A hub admin signs in from a machine with `evo-agents hub login --url https://hub.acme.example`, registers the
   client's projects (`evo-agents hub project register` in each harness) and grants its people their roles
   (`evo-agents hub admin grant`).
3. The hub admin sets the branding and turns off the features the client does not use, in Administration, Branding
   and Features, or through `/v1/admin/branding` and `/v1/admin/features` (`docs/hub.md`, section Branding and
   features). Each change is a version that a rollback undoes.
4. The hub admin installs the plugins (next section).

## Installing plugins

A publisher's CI packs and signs each bundle (`docs/plugins.md`, section Signing in CI), and keeps the bundle and its
`.sigstore.json` (or `.sig.json`) together. With the publisher in `EVO_HUB_PLUGIN_PUBLISHERS`, a hub admin publishes
and installs the plugin, and an admin of each project that wants it enables it there:

```sh
evo-agents hub plugin publish sandbox-0.1.0.tar.gz        # the signature file beside it; a refusal names its reason
evo-agents hub plugin install sandbox                     # the newest version that runs on this release
evo-agents hub plugin config sandbox --instance --set greeting=hello          # the hub's own layer, if it takes one
evo-agents hub plugin enable sandbox --project acme-web   # prints the permissions it asks for, then grants them
evo-agents hub plugin config sandbox --project acme-web --set greeting=hi
printf %s "$API_TOKEN" | evo-agents hub plugin config sandbox --project acme-web --secret api_token
evo-agents hub plugin show sandbox                        # versions, signers, what it asks for
curl -fsS -H "authorization: bearer $(cat ~/.evo/hub/token)" https://hub.acme.example/v1/plugins/sandbox/backend
```

The web does the same in Administration, Plugins (the store, the trusted publishers, the upgrades waiting) and on
each project's Plugins page (enabling with the consent screen, and the config form). A refused bundle answers 422
with its reason in parentheses: `unsigned`, `altered`, `untrusted` (a publisher, workflow or key the operator did not
list), `invalid`, `incompatible` or `malformed`. A new version is published and then upgraded to with
`evo-agents hub plugin upgrade`. When the new version asks for more permissions, the plugin keeps what each project
granted before, and the new ones wait under `pending` until an admin of the project enables the plugin again.

## Two hubs on one machine

Each instance is a compose project of its own (`docker compose -p <name>`, or an app of its own on the platform),
with its own database, bucket and volumes. The services reach each other by aliases on the project's own network
(`evo-agents-hub-api`, `evo-agents-hub-plugins`), so two hubs on one Docker host never reach each other's api or
plugin host. To try a second hub beside a first with the local stack of `docs/hub.md` (section Running the stack
locally), give it another project name, other host ports, and a platform network of its own: `docker-compose.dev.yml`
gives that network the same name in every project, where both stacks' Postgres would answer to `postgres`.

```yaml
# docker-compose.second.yml
networks:
  platform:
    name: evo-agents-hub-second-platform
```

```sh
EVO_HUB_DEV_API_PORT=8180 EVO_HUB_DEV_WEB_PORT=3100 docker compose -p evo-agents-hub-second \
  -f deploy/hub/docker-compose.yml -f deploy/hub/docker-compose.dev.yml -f docker-compose.second.yml \
  --env-file second.env up -d --build
```

`second.env` is a copy of `deploy/hub/.env.example` with this hub's own admins, publishers, secrets and bucket name.
