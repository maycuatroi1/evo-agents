# The evo-agents hub

The hub is one server a team shares for four things that otherwise live on single machines: Claude Code's memory
files, the plans of each harness, skill directories, and knowledge graphs. Machines talk to it with
`evo-agents hub ...` commands and through the `evo-hub` Claude Code plugin; people read it in a browser.

A deployment has five parts:

| Part | What it runs | State |
| --- | --- | --- |
| api | `evo-agents hub serve`: the HTTP API under `/v1` and the MCP endpoint `/mcp` | none; `/cache` is a cache |
| worker | `evo-agents hub worker`: background jobs from a queue in Postgres | none; `/cache` is a cache |
| web | the Next.js server of `web/` | none |
| Postgres | users, tokens, projects, grants, memories, plans, skills, graph builds, the job queue, the audit trail | all of it |
| blob store | Cloudflare R2 (any S3 API works): skill bundles, run logs, source files and built graphs | content-addressed bytes |

A reverse proxy in front sends `/v1` and `/mcp` to `api:8080` and every other path to `web:3000`, on one domain.
The browser then sees the API on the web's own origin, and the session cookie reaches both.

## The API

Every route lives under `/v1` and answers JSON. Errors have one shape, `{error, message, request_id}`, whatever went
wrong: a 404, a validation failure or an unhandled exception never answers with HTML or a traceback, and the
`request_id` is the one in the api's log line for that request.

A request carries one of two credentials:

- a machine token (`evh_...`) as `Authorization: Bearer`, which `evo-agents hub login` stores on a machine;
- a web session (`evs_...`) in the `evo_hub_session` cookie. A write made with the cookie (POST, PUT, PATCH,
  DELETE) also needs the `X-Evo-CSRF` header, whose value `GET /v1/auth/web/csrf` hands out.

Only the health checks, the OpenAPI document and the first steps of sign-in (`/v1/auth/config`, `/v1/auth/github`,
`/v1/auth/web/login`, `/v1/auth/web/callback`) answer without one. A route added later needs a credential unless it
is added to that list in `evo_agents/hub/server/security.py`. Postgres keeps only the SHA-256 of a token. A token
nobody uses for 90 days expires, and each use moves the expiry forward.

| Area | Routes |
| --- | --- |
| health | `GET /v1/health`, `GET /v1/health/live` |
| sign-in | `/v1/auth/config`, `/v1/auth/github`, `/v1/auth/whoami`, `/v1/auth/logout`, `/v1/auth/web/{login,callback,csrf,logout}` |
| tokens | `GET /v1/tokens`, `DELETE /v1/tokens/{id}` |
| admin | `/v1/admin/users`, `/v1/admin/stats`, `/v1/admin/projects/{project}/grants/{login}`, `/v1/admin/audit`, `/v1/admin/tokens` |
| projects | `GET /v1/projects`, `GET` and `PUT /v1/projects/{project}` |
| plans | `/v1/projects/{project}/plans`, `.../plans/{plan_id}` (`GET`, `PUT`, `PATCH`), `.../revisions`, `.../diff`, `.../complete` |
| memories | `/v1/memories` (`GET`, `PUT`), `/v1/memories/search`, `/v1/memories/{id}`, `.../revisions` |
| skills | `GET /v1/skills`, `/v1/skills/global/{name}` and `/v1/skills/projects/{project}/{name}`, each with `/versions` and `/bundle` |
| blobs | `POST /v1/blobs/uploads`, `POST /v1/blobs/commit` |
| knowledge graphs | `/v1/kg/{project}/config`, `.../runs`, `.../blobs/check`, `.../builds`, `.../tools/{tool}`, and the web's `.../graph`, `.../nodes`, `.../node`, `.../neighbourhood` |

`/mcp` speaks MCP's Streamable HTTP transport, statelessly: each POST carries one JSON-RPC message and gets one JSON
answer. It takes machine tokens only, and the `Host` header must be the host of `EVO_HUB_PUBLIC_URL` or a loopback
name. Runtimes reach it through `evo-agents hub mcp`, a stdio proxy that adds the token, the session's project
(`X-Evo-Project`) and its sink (`X-Evo-Sink`, `claude-code@anthropic` by default). Its 15 tools are the seven
`kg_*` tools of `evo-agents kg serve` plus `memory_search`, `memory_get`, `memory_write`, `plan_list`, `plan_show`,
`plan_step`, `skill_list` and `hub_projects`, and they follow the same rules as the REST routes.

### OpenAPI

The api serves its OpenAPI document at `/v1/openapi.json`. `evo-agents hub openapi -o openapi.json` writes the same
document without a database or any `EVO_HUB_*` variable, so the output depends only on the checkout. The web's
typed client comes from it (`pnpm gen:api` in `web/`), and CI fails when the committed `schema.d.ts` differs from
what that script generates.

### The command line contract

`evo-agents hub contract print` prints `{"version": 1, "commands": {...}, "openapi": {...}}`: every `evo-agents hub`
command with its arguments, options and the keys its `--json` output holds, plus the OpenAPI document. This is the
owner's side of the seam `hub-cli-v1`: a consumer, such as evo-cli running `evo-agents hub plan ...`, checks the
argv it builds and the keys it reads against this document. `tests/hub/golden/cli-contract.json` holds the expected
copy, so any change to a command, an option, a `--json` key or the API shows up as a diff in review.

`evo-agents hub contract check README.md docs/hub.md` reads every `evo-agents hub ...` command in the code spans and
fenced code blocks of markdown files and reports a subcommand or option that does not exist, an option without its
value, or a value outside an option's choices. It needs only the core package; `--contract FILE` checks against a
saved `contract print` output instead.

## Signing in

Sign-in goes through a GitHub OAuth App that belongs to the deployment. In the App's settings, turn on the device
flow (the CLI uses it) and set the callback URL to `EVO_HUB_PUBLIC_URL` followed by `/v1/auth/web/callback` (the
browser uses it). The hub asks GitHub for nothing beyond the login (scope `read:user`) and never keeps a GitHub
token: it asks GitHub whose token it is, issues its own token, and drops GitHub's.

From a machine:

```sh
evo-agents hub login --url https://hub.example.org   # GitHub device flow; prints a code to enter on github.com
evo-agents hub whoami                                 # login, admin or not, grants
evo-agents hub token list --all                       # this login's tokens, revoked and expired ones too
evo-agents hub token revoke 42
evo-agents hub logout                                 # revokes this machine's token and deletes it here
```

The token and the hub URL stay in `~/.evo/hub` (mode 0700, files 0600). A hub URL must be https, or http to a
loopback address. No command prints a token.

In the browser, the web sends you to GitHub (`/v1/auth/web/login`, with state and PKCE), and the callback sets the
session cookie (HttpOnly, Secure, SameSite=Lax). Web sign-in needs four variables: the client id and secret, the
session secret, and the public URL. Until all four are set, its routes answer 503 while the CLI's device flow keeps
working with the client id alone. When the client secret is set, the hub also checks with GitHub that a CLI's token
was issued to this App, so a personal token or another App's token signs nobody in.

The logins in `EVO_HUB_ADMINS` are hub admins. An admin registers projects and grants roles; a login can be granted
before its first sign-in, and the grant waits for it:

```sh
evo-agents hub admin grant alice demo --role writer --max-level internal
evo-agents hub admin revoke alice demo
evo-agents hub admin users
evo-agents hub admin stats      # rows in every table
```

The audit trail and every user's tokens are on the web's admin pages (`/v1/admin/audit`, `/v1/admin/tokens`).

## Projects and who sees what

A project on the hub is a harness. `evo-agents hub project register` (run in the harness, or with its path) sends
what the hub needs from `knowledge.yaml` and `harness.yaml`: the label ladder (levels and locations), the sinks with
their clearances, the default label of the project's memories and plans (that of its harness source), its repos, and
where the harness and repos sit relative to a workspace. Registering a new project takes a hub admin; registering it
again after the harness changed takes a hub admin or the project's admin role. `evo-agents hub registry pull` writes
the projects you see into this machine's harness registry, placed under your workspace.

A grant gives a login one role on a project, `reader`, `writer` or `admin` (each may do what the ones before it
may), and a max level on that project's ladder. Every object in a project carries a label `{level, location,
integrity, projects}`, and two rules decide everything:

- **Reading.** An object labelled L, read through sink S by a member whose grant reaches level M, is visible only
  when L is below both M and the clearance of S, and L names no project other than this one. The sink is the
  runtime the data goes to: a Claude Code session reads through `claude-code@anthropic` unless it names another.
  The web involves no sink, so it shows each member what their grant alone allows.
- **Pushing.** The member needs the writer role, and L must be below the clearance of the project's sink of kind
  `hub`. A project whose `knowledge.yaml` declares no such sink takes no push at all.

Both rules fail closed: a level the ladder lacks counts as the highest, and a label the hub cannot read hides its
object. Something you may not see answers exactly like something that does not exist. A hub admin without a grant
on a project manages it (registration, grants) but reads and pushes nothing in it.

Memories add one rule on top: `project` and `reference` memories go to every member the read rule lets through, while
`user` and `feedback` memories go only to the person who wrote them. Memories outside any project are personal and
only their owner sees them.

## Plans and their copies in git

A harness hands its plans to the hub by naming the project in `harness.yaml`:

```yaml
hub:
  project: demo
```

From then on the hub holds each plan as a row with a revision, and every write appends a revision with who made it.
The files under `plans/active/` and `plans/completed/` become read-only copies the hub writes. Change a plan with the
hub commands, `evo harness step` (evo-cli), or the `plan_step` MCP tool, never by editing the YAML:

```sh
evo-agents hub plan import .                          # first time: push every plan the hub does not hold
evo-agents hub plan list --area active
evo-agents hub plan show rollout                      # the plan as its copy reads
evo-agents hub plan step rollout 2 done --evidence "pytest: 12 passed"
evo-agents hub plan patch rollout --step 2 --set note="merged at abc1234"
evo-agents hub plan history rollout
evo-agents hub plan complete rollout                  # every step done: move it to completed
evo-agents hub plan export . --commit                 # write the copies, commit exactly the ones that changed
```

`--project` names the hub project when the current directory is not inside the harness. A write names the revision it
read. When someone else wrote in between, the hub answers 409 with the current plan, and `plan step` and `plan patch`
read it again and retry; pass `--if-revision N` to fail instead. `plan put FILE --if-revision N` replaces a whole
plan from a file.

A copy looks like this:

```yaml
# Mirror of evo-agents hub plan rollout, revision 3. Do not edit; use evo harness step or evo-agents hub plan.
id: rollout
title: Roll out the hub
status: active
steps:
  - id: 1
    title: Write the docs
    repo: app
    what: Describe the API.
    depends_on: []
    verify: pytest -q
    status: done
    done_at: '2026-10-04'
    evidence: 'pytest: 12 passed'
hub:
  project: demo
  revision: 3
  digest: sha256:3627610040aec7b4f3aebc566bca63dfdda96d1a57ba85b780047e5f80080987
```

The first line names the plan and revision. The `hub` key at the end names the project, the revision and the digest:
the SHA-256 of the canonical JSON of the plan without the `hub` key, so key order, comments and YAML styles do not
change it. The text depends on the plan alone: keys come in the order of `plan.schema.json`, long text is a folded
block, and multi-line text a literal block, so two machines exporting the same revision write the same bytes.

`evo-agents harness validate` reports a copy whose digest no longer matches its content, and says both ways out:
push the edit with `plan put` or restore the hub's copy with `plan export`. The evo-hub plugin's SessionStart hook
exports the copies of the session's harness without committing, and leaves a file edited by hand as it is and names
it, so it never overwrites someone's work.

## Memories, skills and knowledge graphs

**Memories.** `evo-agents hub memory push` and `evo-agents hub memory pull` sync Claude Code's memory files for a
directory (the current one by default, `--all` for every one on this machine). A directory that is the harness root
or a repo of a hub project belongs to that project; any other directory is personal. When both sides changed a file,
the hub's version keeps the name and this machine's lands next to it as `<name>.conflict-<host>.md`. Deletions cross
only with `--prune`. `evo-agents hub memory search "a phrase"` searches what you see. The plugin pulls at
SessionStart and pushes at Stop.

**Skills.** `evo-agents hub skills publish skills/house-style --scope global` packs a skill directory (10 MiB at
most) and publishes a new version; `--scope project:demo` publishes it to a project. `evo-agents hub skills sync`
writes the latest version of every skill you see into the skills directories of each runtime on the machine
(`claude`, `agents`, `codex`, `cursor`, `gemini`), and project skills into the harness's `.claude/skills` and
`.agents/skills`. It changes only directories it wrote itself (`--adopt` also takes over copies made by hand),
saves a copy before replacing anything (under `~/.evo/hub/backups/`), and `--check` reports without writing.

**Knowledge graphs.** A project's graph is built on the hub from the run logs machines push. `evo-agents hub kg
push --project demo` (or `evo-agents kg sync --push`) sends the connector runs the hub lacks; a run whose label the
hub sink does not clear stays on the machine. `evo-agents hub kg build --project demo --wait` queues a build and waits
for it, and `evo-agents hub kg builds --project demo` lists them. Once a build succeeded, `evo-agents kg serve
--backend auto` and the hub's `/mcp` answer the `kg_*` tools from it, filtered by the read rule.

## Blobs on R2

Postgres holds metadata; the blob store holds bytes. The bucket stays private, and the access key the hub gets should
be limited to that one bucket. Keys:

- `blobs/sha256/<sha256>`: a blob, written only by the hub and only with bytes whose SHA-256 the hub computed. A
  blob never changes and the hub never deletes one. The `blobs` table says which projects hold each.
- `uploads/<upload_id>` and `uploads/<upload_id>.sealed`: an upload in progress, and the hub's own copy of it.

An upload takes three calls. The client asks `POST /v1/blobs/uploads` for presigned PUT URLs (valid 15 minutes, for
the declared size only), PUTs the bytes straight to R2, then calls `POST /v1/blobs/commit`. The hub reads each upload
back, hashes it, and copies it under `blobs/sha256/` only when size and hash match what the client declared. A blob
another project holds must still be uploaded and checked, so knowing a hash gives nobody another project's bytes.
Limits per kind: 10 MiB for a skill bundle, 256 MiB for a run log, 64 MiB for a source file.

Downloads work the same way in reverse: after the read check, the hub hands out a presigned GET. Machines therefore
need to reach the R2 endpoint as well as the hub. The web navigates to the URL instead of fetching it, so the bucket
needs no CORS rule. When R2 does not answer, the blob routes answer 503 and `/v1/health` reports `r2: unavailable`,
while the rest of the API and the `kg_*` tools of graphs already in the api's cache keep working.

## Worker and queue

The worker runs jobs from a [procrastinate](https://procrastinate.readthedocs.io/) queue that lives in the hub's own
Postgres database; there is no separate broker. The api only defers jobs. Jobs:

- `hub.kg_build`: one build of a project's graph. Builds of one project run one at a time, and at most one waits.
- `hub.recover_kg_builds`, every 5 minutes: fails builds whose worker stopped sending heartbeats and queues a new one.
- `hub.cleanup_uploads`, hourly: removes uploads nobody committed within 24 hours.
- `hub.prune_jobs`, daily: removes finished jobs older than 14 days.

`evo-agents hub worker --data-dir /cache --concurrency 2` runs two jobs at a time (default 1). On SIGTERM it takes no
new job and gives running ones 25 seconds, so give its container a stop grace period above that (the compose file
uses 40 s). It exits 1 when it loses Postgres, which lets the restart policy bring it back. Graph builds use CPU and
memory, so the compose file caps the worker with `EVO_HUB_WORKER_CPUS` and `EVO_HUB_WORKER_MEMORY`. The worker
refuses to start without a blob store, since builds read from it and write to it.

## Deploying

GitHub Actions (`.github/workflows/images.yml`) builds two images. A push to a branch builds both and pushes
nothing; a `v*` tag pushes them to GHCR with the version as tag:

- `ghcr.io/maycuatroi1/evo-agents-hub:<version>`, from `deploy/hub/Dockerfile`: api and worker. It runs as a non-root
  user, serves on 8080, and its HEALTHCHECK calls `/v1/health/live`.
- `ghcr.io/maycuatroi1/evo-agents-hub-web:<version>`, from `web/Dockerfile`: the Next.js standalone server on 3000.
  `next build` fixes the `/v1` rewrite into the image, and the build argument `EVO_HUB_API_INTERNAL_URL` sets its
  target, `http://api:8080` by default. Build with `--build-arg EVO_HUB_API_INTERNAL_URL=<url>` for another address.

`deploy/hub/docker-compose.yml` runs the three services from those images and builds nothing. Postgres is not in it:
point `EVO_HUB_DSN` at a database the platform manages, reachable on the Docker network `EVO_HUB_PLATFORM_NETWORK`
(default `dokploy-network`). The volumes `api-cache` and `worker-cache` are caches; deleting them loses nothing. Set
the variables in the platform's environment, never in a committed file. `deploy/hub/.env.example` lists them:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `EVO_HUB_VERSION` | compose | image tag, a released version such as `0.2.0` |
| `EVO_HUB_DSN` | api, worker | `postgresql://` URI of the hub database (required) |
| `EVO_HUB_ADMINS` | api | GitHub logins of hub admins, comma-separated |
| `EVO_HUB_GITHUB_CLIENT_ID` | api | the OAuth App's client id; without it nobody can sign in |
| `EVO_HUB_GITHUB_CLIENT_SECRET` | api | the OAuth App's secret, for web sign-in and the App check |
| `EVO_HUB_SESSION_SECRET` | api | at least 32 random characters; signs session and CSRF values |
| `EVO_HUB_PUBLIC_URL` | api | the URL browsers use, such as `https://hub.example.org` |
| `EVO_HUB_S3_ENDPOINT`, `EVO_HUB_S3_BUCKET`, `EVO_HUB_S3_ACCESS_KEY_ID`, `EVO_HUB_S3_SECRET_ACCESS_KEY` | api, worker | the blob store, all four or none; for R2 the endpoint is `https://<account id>.r2.cloudflarestorage.com` |
| `EVO_HUB_SENTRY_DSN` | api, worker | optional error reporting |
| `EVO_HUB_LOG_LEVEL` | api, worker | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |
| `EVO_HUB_WORKER_CPUS`, `EVO_HUB_WORKER_MEMORY` | compose | worker limits, default `2` and `4g` |
| `EVO_HUB_API_INTERNAL_URL` | web | where the web server reaches the api, default `http://evo-agents-hub-api:8080` (the api's network alias) |
| `EVO_HUB_WEB_TIME_ZONE` | web | time zone of dates rendered on the server, default `Asia/Ho_Chi_Minh` |
| `EVO_HUB_PLATFORM_NETWORK` | compose | the external network the database is on |

The server also reads `EVO_HUB_POOL_MIN_SIZE`, `EVO_HUB_POOL_MAX_SIZE` and `EVO_HUB_POOL_TIMEOUT` (the connection
pool, defaults 1, 10 and 10 seconds); add them to the environment block of the compose file to change them. A missing
or malformed variable stops the process with a log line naming it.

The reverse proxy routes the public domain: `/v1` and `/mcp` to the api on port 8080, everything else to the web on
port 3000. `EVO_HUB_PUBLIC_URL` must be that domain, because the web sign-in callback and the `/mcp` host check both
use it.

### Running the stack locally

`deploy/hub/docker-compose.dev.yml` adds, on top of the production file, image builds from this checkout, Postgres
16, moto as the S3 store, and a one-off service that creates the bucket. `deploy/hub/.env.example` works with it as
it is:

```sh
docker compose -f deploy/hub/docker-compose.yml -f deploy/hub/docker-compose.dev.yml \
  --env-file deploy/hub/.env.example up -d --build
curl -fsS localhost:8080/v1/health        # {"status": "ok", "db": "ok", "r2": "ok", ...}
open http://localhost:3000
docker compose -f deploy/hub/docker-compose.yml -f deploy/hub/docker-compose.dev.yml down -v
```

Only the api (8080) and the web (3000) get host ports, on 127.0.0.1; `EVO_HUB_DEV_API_PORT` and
`EVO_HUB_DEV_WEB_PORT` move them. Postgres and moto stay inside the stack, so presigned URLs (`http://moto:5000/...`)
work only from containers: to push skills or graphs from this machine, use the Playwright stack in `web/README.md`.
Sign-in needs a GitHub OAuth App of your own; fill in the three sign-in variables in a copy of the file.

## Operations

**Migrations.** The api and the worker migrate the database when they start. Whoever starts first takes a Postgres
advisory lock and applies every pending revision in one transaction; the other waits, finds the schema current, and
changes nothing. `evo-agents hub migrate --dsn "$EVO_HUB_DSN"` does the same on its own, for example before switching
traffic to a new release. A database at a revision the running code does not know, written by a newer release, is
refused before anything runs, so going back to an older release means restoring a backup taken before the upgrade.

**Backup.** Postgres holds every piece of state, so a backup is a dump of the database plus a copy of the bucket's
`blobs/` prefix:

```sh
pg_dump --format=custom --no-owner --file hub-$(date +%Y%m%d).dump "$EVO_HUB_DSN"
rclone sync r2:<bucket>/blobs backup:<bucket>/blobs    # any S3-capable copy tool works
```

Dump first, then copy the bucket. Blobs never change and are never deleted, so a bucket copy taken after the dump
holds every blob the dump refers to. Skip `uploads/` (transient) and the cache volumes.

**Restore.** Stop the api and the worker. Restore the dump into an empty database with `pg_restore --no-owner
--dbname "$EVO_HUB_DSN" hub-<date>.dump`, copy `blobs/` back into the bucket if it was lost, and start the services
with a release at least as new as the one that wrote the dump. A newer release migrates the restored database
forward. The caches fill again on their own. The job queue is part of the dump, so builds that were queued run, and
the recovery job fails and queues again any build that was running when the dump was taken.

**Health.** `GET /v1/health` reports the package version, the schema revision, `db` and `r2`. It answers 503 and
lists the failed component when Postgres or the bucket does not answer within 3 seconds; a hub without the S3
variables reports `r2: unconfigured` and stays healthy. `GET /v1/health/live` only says the process serves requests.
The container healthcheck uses the live route on purpose: an R2 outage must not get the api restarted while it still
answers from its cache. Point uptime monitoring at `/v1/health`.

**Logs.** The api and the worker write JSON lines to stderr. The api writes one access line per request, with its
id and without the query string, and one line per MCP tool call with its name and outcome but not its arguments.
Tokens, DSN passwords, S3 keys and presigned signatures are masked before a line is written. Set
`EVO_HUB_SENTRY_DSN` to also send errors to Sentry.

**Upgrades.** Set `EVO_HUB_VERSION` to the new release, pull, and recreate the services; the api migrates on start.
Take a backup first when the release notes mention a migration.
