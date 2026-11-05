# Host a hub on one machine

This page takes you from an empty Linux server to a hub your team can sign in to: the hub, its database, its blob
store and a reverse proxy with HTTPS certificates, all in Docker on that one machine. For a hub on a platform such as
Dokploy, with a managed database or Cloudflare R2, read [Deploying a hub for a client](hub-deployment.md) instead.

## What you need

- A Linux server with Docker Engine and the Compose plugin v2 or newer, ports 80 and 443 open to the internet. At rest the stack uses about 1.5 GB of memory; each knowledge graph build can take up to 4 GB,
  and two run at once. Give it at least 4 CPUs and 8 GB, or lower `EVO_HUB_WORKER_MEMORY` and `EVO_HUB_WORKER_CPUS`
  in the `.env` file below on a smaller machine.
- Two DNS names that point at the server, for example `hub.example.org` for the hub and `s3.hub.example.org` for its
  blob store. Workers and the CLI upload and download through the second one.
- A GitHub account to create the OAuth App people sign in with.

## 1. Create the GitHub OAuth App

On GitHub, open Settings, Developer settings, OAuth Apps, and choose New OAuth App:

- Homepage URL: `https://hub.example.org`
- Authorization callback URL: `https://hub.example.org/v1/auth/web/callback`

Create it, tick **Enable Device Flow**, and generate a client secret. Keep the client id and the secret for step 2.

## 2. Fill in the variables

On the server:

```sh
git clone --depth 1 https://github.com/maycuatroi1/evo-agents.git
cd evo-agents
cp deploy/hub/single.env.example deploy/hub/.env
chmod 600 deploy/hub/.env
```

Edit `deploy/hub/.env`:

- `EVO_HUB_VERSION`: the newest [release](https://github.com/maycuatroi1/evo-agents/releases), without the `v`.
- `EVO_HUB_DOMAIN` and `EVO_HUB_S3_DOMAIN`: your two names.
- `EVO_HUB_ADMINS`: your GitHub login, and those of the other hub admins, separated by commas.
- `EVO_HUB_GITHUB_CLIENT_ID` and `EVO_HUB_GITHUB_CLIENT_SECRET`: from step 1.
- The five secrets. Generate each one here; never reuse a value from an example or from another hub:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # EVO_HUB_SESSION_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # run once for each of the other four
```

`deploy/hub/.env` is in `.gitignore`. Keep a copy of it somewhere safe: without `EVO_HUB_SECRETS_KEY` the hub cannot
read the credentials it stores for runs.

## 3. Start it

The stack is three compose files and the `.env` file. A shell function saves typing them; the rest of this page uses
it:

```sh
hub_compose() {
  docker compose -f deploy/hub/docker-compose.yml -f deploy/hub/docker-compose.seaweedfs.yml \
    -f deploy/hub/docker-compose.single.yml --env-file deploy/hub/.env "$@"
}
hub_compose up -d
curl -fsS https://hub.example.org/v1/health
```

The first start pulls the images and gets the two certificates from Let's Encrypt, which takes a few minutes. The
health check answers `"status": "ok"`, `"db": "ok"` and `"r2": "ok"` (the blob store) once the hub is ready.
`hub_compose ps` shows each service; `hub_compose logs proxy` shows certificate errors, most often a DNS name that
does not point at the server yet.

Open `https://hub.example.org` and sign in with GitHub. Then follow [Getting started](getting-started.md), from
[Install and sign in](getting-started.md#install-and-sign-in), with your hub's URL.

## Upgrade

Set `EVO_HUB_VERSION` to the new release and run `hub_compose up -d`. The api and the worker migrate the database as they
start. Read the release notes first: before 1.0, a release can change commands and settings.

## Back up

The database and the blob store are the hub's whole state; the other volumes are caches. Back up both, at the same
time:

```sh
hub_compose exec -T postgres pg_dump -U hub -Fc evo_hub > evo_hub-$(date +%F).dump
docker run --rm -v evo-agents-hub_seaweedfs-data:/data:ro -v "$PWD":/out alpine:3.20 \
  tar czf /out/seaweedfs-$(date +%F).tar.gz -C /data .
```

Keep `deploy/hub/.env` with them.

## Try it on a laptop

With `EVO_HUB_DOMAIN=hub.localhost` and `EVO_HUB_S3_DOMAIN=s3.localhost` the stack needs no DNS and no GitHub callback
on the internet, and the proxy signs both names with its own certificate authority. Browsers show a certificate
warning once; the CLI and workers on the same machine need the authority's root certificate:

```sh
hub_compose cp proxy:/data/caddy/pki/authorities/local/root.crt caddy-root.crt
export SSL_CERT_FILE=$PWD/caddy-root.crt
```

When ports 80 and 443 are taken, set `EVO_HUB_HTTP_PORT` and `EVO_HUB_HTTPS_PORT`, and add the port to
`EVO_HUB_PUBLIC_URL`, `EVO_HUB_S3_ENDPOINT` and the OAuth App's URLs.

## Optional parts

`deploy/hub/.env.example` describes every other variable: the model gateway, Telegram and push notifications, Sentry,
the GitHub App for pushes. Add the ones you need to `deploy/hub/.env` and run `hub_compose up -d` again.
