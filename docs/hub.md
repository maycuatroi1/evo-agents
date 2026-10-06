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
| Postgres | users, tokens, projects, grants, memories, plans, skills, graph builds, workers and runs, the decisions of plan runs and the notifications of members, the job queue, the audit trail | all of it |
| blob store | Cloudflare R2 (any S3 API works): skill bundles, run logs, source files and built graphs | content-addressed bytes |

A reverse proxy in front sends `/v1` and `/mcp` to `api:8080` and every other path to `web:3000`, on one domain.
The browser then sees the API on the web's own origin, and the session cookie reaches both.

## The API

Every route lives under `/v1` and answers JSON. Errors have one shape, `{error, message, request_id}`, whatever went
wrong: a 404, a validation failure or an unhandled exception never answers with HTML or a traceback, and the
`request_id` is the one in the api's log line for that request.

A request carries one of three credentials:

- a machine token (`evh_...`) as `Authorization: Bearer`, which `evo-agents hub login` stores on a machine;
- a web session (`evs_...`) in the `evo_hub_session` cookie. A write made with the cookie (POST, PUT, PATCH,
  DELETE) also needs the `X-Evo-CSRF` header, whose value `GET /v1/auth/web/csrf` hands out;
- a worker token (`evw_...`) as `Authorization: Bearer`, which a worker gets once when it joins or registers. It
  works only on `/v1/worker/*`, where machine tokens and web sessions get 403, and gets 403 everywhere else
  (`docs/workers.md`). Revoking it (`DELETE /v1/tokens/{id}`, or `DELETE /v1/admin/tokens/{id}`) revokes its worker
  too and releases the runs the worker holds.

Only the health checks, the OpenAPI document, the first steps of sign-in (`/v1/auth/config`, `/v1/auth/github`,
`/v1/auth/web/login`, `/v1/auth/web/callback`) and a worker's join with a pairing code (`/v1/worker/join`) answer
without one. A route added later needs a credential unless it is added to that list in
`evo_agents/hub/server/security.py`. Postgres keeps only the SHA-256 of a token. A token nobody uses for 90 days
expires, and each use moves the expiry forward. The join is public, so it is limited: 10 refused pairing codes from one
client address within 10 minutes, and that address gets 429 with `Retry-After` (see `EVO_HUB_FORWARDED_ALLOW_IPS`
below for the address behind a proxy).

The credential check reads HTTP requests only. A websocket under `/v1` is closed during its handshake, which the
client sees as a 403, unless its route checks a credential of its own and is listed in `SELF_CHECKED_WEBSOCKETS` of
the same file. The two ends of a run's web terminal are the only ones: the browser's checks the session cookie, the
Origin and a CSRF value, and the worker's its `evw_` token (`docs/workers.md`).

| Area | Routes |
| --- | --- |
| health | `GET /v1/health`, `GET /v1/health/live` |
| sign-in | `/v1/auth/config`, `/v1/auth/github`, `/v1/auth/whoami`, `/v1/auth/logout`, `/v1/auth/web/{login,callback,csrf,logout}` |
| tokens | `GET /v1/tokens`, `DELETE /v1/tokens/{id}` |
| admin | `/v1/admin/users`, `/v1/admin/stats`, `/v1/admin/projects/{project}/grants/{login}`, `/v1/admin/audit`, `/v1/admin/tokens`, `/v1/admin/kg/prune` |
| projects | `GET /v1/projects`, `GET` and `PUT /v1/projects/{project}` |
| plans | `/v1/projects/{project}/plans`, `.../plans/{plan_id}` (`GET`, `PUT`, `PATCH`), `.../revisions`, `.../diff`, `.../complete` |
| memories | `/v1/memories` (`GET`, `PUT`), `/v1/memories/search`, `/v1/memories/{id}`, `.../revisions` |
| skills | `GET /v1/skills`, `/v1/skills/global/{name}` and `/v1/skills/projects/{project}/{name}`, each with `/versions` and `/bundle` |
| blobs | `POST /v1/blobs/uploads`, `POST /v1/blobs/commit` |
| knowledge graphs | `/v1/kg/{project}/config`, `.../runs`, `.../blobs/check`, `.../builds`, `.../tools/{tool}`, and the web's `.../graph`, `.../nodes`, `.../node`, `.../neighbourhood` |
| workers | `POST /v1/workers/pairings`, `GET /v1/workers/pairings/{id}`, `POST /v1/worker/join`, `GET` and `POST /v1/workers`, `GET /v1/workers/{id}`, `POST /v1/workers/{id}/{drain,undrain,revoke}` |
| runs | `/v1/projects/{project}/plans/{plan_id}/ready-steps`, `GET` and `POST /v1/projects/{project}/runs`, `POST /v1/projects/{project}/plan-runs`, `.../runs/{id}`, `.../events`, `.../stream`, `.../diff`, `.../messages`, `.../{cancel,approve,rerun,takeover,handback}` |
| decisions | `GET /v1/projects/{project}/decisions`, `.../decisions/{id}`, `POST .../decisions/{id}/answer` |
| notifications | `GET /v1/me/notifications`, `GET /v1/me/notifications/count`, `POST /v1/me/notifications/read` |
| worker protocol | `/v1/worker/{claim,heartbeat}`, `/v1/worker/runs/{id}/{state,events,inbox,uploads,blobs,plan,decisions,notices}`, `/v1/worker/runs/{id}/steps/{key}` |

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
command, and the four commands the agent of a plan run uses (`evo-agents worker step`, `ask`, `notify` and `plan`,
keyed `worker step` and so on), with its arguments, options and the keys its `--json` output holds, plus the OpenAPI
document. This is the owner's side of the seam `hub-cli-v1`: a consumer, such as evo-cli running `evo-agents hub
plan ...` or a skill of agent-skills that tells an agent to run `evo-agents worker step`, checks the argv it builds
and the keys it reads against this document. `tests/hub/golden/cli-contract.json` holds the expected copy, so any
change to a command, an option, a `--json` key or the API shows up as a diff in review.

`evo-agents hub contract check README.md docs/hub.md` reads every `evo-agents hub ...` command, and every one of the
four `evo-agents worker` commands above, in the code spans and fenced code blocks of markdown files and reports a
subcommand or option that does not exist, an option without its value, or a value outside an option's choices. The
other `evo-agents worker` commands are not in the contract and are not read. It needs only the core package;
`--contract FILE` checks against a saved `contract print` output instead.

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
it, so it never overwrites someone's work. In the session of a run's agent on a worker (`EVO_RUN_ID` set by the
daemon) it exports no copy, since the worker commits what the run leaves in its worktree and a run's commits hold only
its own work; its line says so.

## Workers and runs

A worker is a member's own laptop or desktop that runs plan steps for that member. The member dispatches a ready step
and the hub queues a run; a worker of that member claims it, runs the step with Claude Code, opencode or Codex CLI,
and sends its log, state and evidence back over HTTPS, and the hub records the step's progress in the plan. Only the
owner of a worker dispatches runs to it, and dispatching needs the writer role. `docs/workers.md` describes the
protocol, the run states and who may move a run between them.

Schema 0009 holds the workers and runs: `workers`, `worker_projects`, `worker_pairings`, `runs`, `run_events` and
`run_inbox`. Schema 0010 adds plan runs (`runs.kind` is `step` or `plan`, a plan run has `repos` instead of a step key
and a repo, and the states `waiting` and `parked`), the decisions their agents ask (`decisions`) and what reaches the
members (`notifications`, `notification_channels`, `notification_deliveries`), which `docs/notifications.md`
describes. Going back to 0009 deletes the plan runs with their decisions and notifications.

The daemon on the member's machine is the `evo-agents worker` command group, which needs the `worker` extra
(`uv tool install 'evo-ak[worker]'`). It is not `evo-agents hub worker`, the hub's own job worker (see Worker and
queue below). A machine joins with a pairing code from the web's Workers page, or registers directly once signed in
with `evo-agents hub login`.

Runs are driven from any machine signed in to the hub:

```sh
evo-agents hub run dispatch rollout 2 4 --approval auto       # one run per step, all of them queued or none
evo-agents hub run dispatch rollout 5 --worker mac-mini --runtime codex --mode interactive --timeout 90
evo-agents hub run dispatch rollout 6 --runtime claude-code --model opus
evo-agents hub run plan rollout --worker mac-mini --timeout-h 8  # one plan run: every step not done yet
evo-agents hub run list --state running --state review        # newest first, with the runs in each state
evo-agents hub run show 41
evo-agents hub run logs 41 --follow                           # the live log, until the run ends
evo-agents hub run send 41 "also run ruff before you commit"
evo-agents hub run takeover 41                                # a person drives the agent in a terminal on the worker
evo-agents hub run handback 41                                # the agent goes on headless in the same session
evo-agents hub run approve 41                                 # a run in review: the run and its step are done
evo-agents hub run cancel 41
evo-agents hub run rerun 41                                   # the step again, after a run that ended
```

Every command after `dispatch`, `plan` and `list` takes the id of a run, as `list` shows it, and finds its project as
the plan commands do: `--project`, or `hub.project` in the harness around the current directory. `dispatch` takes
`--runtime` (`any` by default: the first runtime the claiming worker has), `--model` (the model as the runtime names
it, `provider/model` for opencode; the runtime's own choice by default), `--mode` (`headless` by default, or
`interactive`), `--worker` (the id or the name of one of your workers, which pins the runs to it), `--approval`
(`review` by default, which waits for `run approve`; `auto` marks the step done once every verify command the worker
runs again exits 0) and `--timeout` in minutes (5 to 240, 60 by default). A model belongs to one runtime, so the
command line takes `--model` only with `--runtime` naming that runtime and refuses it with `any`, before it asks the
hub; the hub itself does not check the pair.

`run plan` queues a plan run (`POST /v1/projects/{project}/plan-runs`): one run, on one of your workers, whose agent
does every step of the plan not done yet, in a worktree of each repo those steps name, and reports each step as it
goes (`docs/workers.md`, Plan runs). It takes `--worker`, `--runtime`, `--model` and `--mode` as `dispatch` does, and
`--timeout-h`, the hours of agent time the run may use: 2, 4, 8 or 24 (4 by default); the time it waits for your
answer to a decision, or parked, does not count. The hub refuses a plan with no pending step, a plan with an active
run of any kind, and a step not done that names no repo when the plan lists more than one. `list` shows each run's
KIND, `step` or `plan`, and filters by `--state` (repeat it for several), `--plan`, `--step`, `--worker`, `--by` (the
login that dispatched) and `--search`, a page at a time with `--limit` and `--offset`; `show` of a plan run names its
repos, the agent time it used, and when it waited or was parked.

`run logs` prints one line per event: its number, its time in UTC, its kind and what it says. With `--follow` it reads
the run's server-sent events (`GET .../runs/{id}/stream`) until the hub sends `end` once the run is final, and stops
there. A stream that breaks off before that, because a proxy closed it or the hub restarted, is opened again with
`Last-Event-ID` set to the last event read, so no event is missed or printed twice; after five tries in a row to read
on that bring nothing, not even the hub's ping, the command gives up and names the `--after` that reads what came
since. `--json` prints what the hub answered, with the keys the command line contract declares; for `logs` that is
every event read as one object, so it does not go with `--follow`. `send`, `cancel`, `approve`, `takeover`,
`handback` and `rerun` belong to the member who dispatched the run: another member gets 403.

The agent of a plan run asks its owner the decisions it may not take alone (`docs/notifications.md`), and the hub
tells the owner of them, and of pushes to a default branch, in notifications:

```sh
evo-agents hub decision list --state open                     # newest first; --run, --plan, --limit, --offset
evo-agents hub decision show 7                                # the question, its context, the options, the answer
evo-agents hub decision answer 7 --option postgres            # or --text "...", or both; --text - reads stdin
evo-agents hub notifications --unread                         # yours, open decisions first, then newest first
evo-agents hub notifications --read all                       # or --read 12,14
```

`decision list` and `decision show` read the decisions of the plans you may read (`GET /v1/projects/{project}/decisions`,
`.../decisions/{id}`), and find the project as `run` does. `decision answer` (`POST .../decisions/{id}/answer`)
belongs to the member who dispatched the run: another member gets 403, and a decision that is no longer open 409. The
answer names an option of the decision, gives words of your own (at most 4 KiB), or both. It goes to the run's inbox,
which the worker hands to the agent; a parked run is resumed on its worker in its session, as a new run that the
answer names. `notifications` lists your own (`GET /v1/me/notifications`), filtered by `--unread`, `--kind`
(`decision` or `notice`) and `--project`, with `--limit` and `--offset`, and says how many are unread and how many
decisions wait for your answer (`GET /v1/me/notifications/count`). `--read all`, or `--read` with ids, marks them
read (`POST /v1/me/notifications/read`) and lists none, so it takes none of the filters; answering a decision reads
its notification too. Each of these prints the hub's answer with `--json`, `notifications --read` its count of read
and unread.

## Memories, skills and knowledge graphs

**Memories.** `evo-agents hub memory push` and `evo-agents hub memory pull` sync Claude Code's memory files for a
directory (the current one by default, `--all` for every one on this machine). A directory that is the harness root or a
repo of a hub project belongs to that project; any other directory is personal. A directory several projects claim goes
to the strongest claim: the project whose harness root it is, whatever lists it as a repo; else the project `evo-agents
kg bind --project <name> <dir>` bound it to, among those listing it; else the one project listing it. Anything else, two
projects with one harness root among it, is refused rather than guessed, and a memory synced with one project never
moves to another by itself. When both sides changed a file, the hub's version keeps the name and this machine's lands
next to it as `<name>.conflict-<host>.md`. Deletions cross only with `--prune`. `evo-agents hub memory search "a
phrase"` searches what you see. The plugin pulls at SessionStart and pushes at Stop; in the session of a run's
agent on a worker (`EVO_RUN_ID` set) Stop pushes nothing, since nobody reviews that unattended session.

**Skills.** `evo-agents hub skills publish skills/house-style --scope global` packs a skill directory (10 MiB at
most) and publishes a new version; `--scope project:demo` publishes it to a project. `evo-agents hub skills sync`
writes the latest version of every skill you see into the skills directories of each runtime on the machine
(`claude`, `agents`, `codex`, `cursor`, `gemini`), and project skills into the harness's `.claude/skills` and
`.agents/skills`. It changes only directories it wrote itself (`--adopt` also takes over copies made by hand),
saves a copy before replacing anything (under `~/.evo/hub/backups/`), and `--check` reports without writing.

**Knowledge graphs.** A project's graph is built on the hub from the run logs machines push. `evo-agents hub kg push
--project demo` (or `evo-agents kg sync --push`) sends the connector runs the hub lacks; a run whose label the hub sink
does not clear stays on the machine. It reports a line per run and per batch of blobs on stderr. Blobs go in commits of
at most 100 uploads and 256 MiB, each given time in proportion to what it carries. A commit that gets no answer may
still finish on the hub, so the push asks which of its blobs the project holds before calling it failed. A run that
fails for a reason that may pass (no answer, 408, 429, 502, 503, 504) is pushed again from its start, up to three times,
10 and 30 seconds apart; the hub answers what it has already and only the rest is sent. `evo-agents hub kg build
--project demo --wait` queues a build and waits for it, and `evo-agents hub kg builds --project demo` lists them. Once a
build succeeded, `evo-agents kg serve --backend auto` and the hub's `/mcp` answer the `kg_*` tools from it, filtered by
the read rule.

A build's artifact is the graph's SQLite file in the bucket, and its bytes differ from one build to the next even when
the graph does not. A build whose content hash equals that of the project's latest build still holding an artifact
therefore uploads nothing: it points at that artifact, and `artifact_reused_from` names the build that uploaded it.
Every hour the worker deletes the artifacts of graphs older than each project's `EVO_HUB_KG_KEEP_ARTIFACTS` newest (3 by
default; an artifact several builds share counts once, and the newest always stays). A pruned build keeps its row, its
content hash and its counts, gets `artifact_pruned_at`, and loses `artifact_sha256`, so no row names an object that is
gone. Run logs, source blobs and skill bundles are never deleted. A hub admin can run the same retention by hand:

```sh
evo-agents hub kg prune --dry-run              # what would go, for every project; changes nothing
evo-agents hub kg prune --project demo --keep 5
```

## Blobs on R2

Postgres holds metadata; the blob store holds bytes. The bucket stays private, and the access key the hub gets should
be limited to that one bucket. Keys:

- `blobs/sha256/<sha256>`: a blob, written only by the hub and only with bytes whose SHA-256 the hub computed. A
  blob never changes. The hub deletes only built graphs that the retention dropped (see above), and only once no row
  refers to them. The `blobs` table says which projects hold each.
- `uploads/<upload_id>` and `uploads/<upload_id>.sealed`: an upload in progress, and the hub's own copy of it.

An upload takes three calls. The client asks `POST /v1/blobs/uploads` for presigned PUT URLs (valid 15 minutes, for
the declared size only), PUTs the bytes straight to R2, then calls `POST /v1/blobs/commit`. The hub reads each upload
back, hashes it, and copies it under `blobs/sha256/` only when size and hash match what the client declared. A blob
another project holds must still be uploaded and checked, so knowing a hash gives nobody another project's bytes.
Limits per kind: 10 MiB for a skill bundle, 256 MiB for a run log, 64 MiB for a source file.

A commit's time grows with its uploads, because R2 takes about a quarter of a second per call. The hub copies each
upload to a sealed key, streams the copy through SHA-256 in 1 MiB pieces, and copies it to its blob key: three calls
for a blob new to the hub. For a blob the hub already records, the last call only checks that its object is there.
`EVO_HUB_BLOB_CONCURRENCY` (default 32, at most 256) caps how many uploads one process works on at once, shared by
every commit in flight. A commit of 100 uploads therefore takes a few seconds. Release 0.2.0 made five calls per
upload, 16 at a time per commit, and took 40 seconds over 459 uploads. Clients should commit in batches;
`evo-agents hub kg push` sends at most 100 uploads per commit.

Downloads work the same way in reverse: after the read check, the hub hands out a presigned GET. Machines therefore
need to reach the R2 endpoint as well as the hub. The web navigates to the URL instead of fetching it, so the bucket
needs no CORS rule. When R2 does not answer, the blob routes answer 503 and `/v1/health` reports `r2: unavailable`,
while the rest of the API and the `kg_*` tools of graphs already in the api's cache keep working.

## Worker and queue

The worker runs jobs from a [procrastinate](https://procrastinate.readthedocs.io/) queue that lives in the hub's own
Postgres database; there is no separate broker. The api only defers jobs. Jobs:

- `hub.kg_build`: one build of a project's graph. Builds of one project run one at a time, and at most one waits.
- `hub.recover_kg_builds`, every 5 minutes: fails builds whose worker stopped sending heartbeats and queues a new one.
- `hub.prune_kg_artifacts`, hourly at minute 31: deletes the artifacts of graphs older than each project's
  `EVO_HUB_KG_KEEP_ARTIFACTS` newest.
- `hub.recover_runs`, every minute: runs whose worker stopped extending the lease become lost and their step is
  queued again, or fail on their third attempt; runs past their timeout fail; a plan run that waited 24 hours for an
  answer is parked, and one parked for 7 days cancelled (`docs/workers.md`).
- `hub.deliver_notifications`, every minute: hands each notification delivery that is due to its channel's class, and
  tries a failing one again with a backoff, failing it after 5 tries (`docs/notifications.md`).
- `hub.prune_run_events`, daily at 04:13: deletes the events of runs that ended more than `EVO_HUB_RUN_LOG_DAYS` ago.
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
| `EVO_HUB_VERSION` | compose | image tag, a released version such as `0.3.0` |
| `EVO_HUB_DSN` | api, worker | `postgresql://` URI of the hub database (required) |
| `EVO_HUB_ADMINS` | api | GitHub logins of hub admins, comma-separated |
| `EVO_HUB_GITHUB_CLIENT_ID` | api | the OAuth App's client id; without it nobody can sign in |
| `EVO_HUB_GITHUB_CLIENT_SECRET` | api | the OAuth App's secret, for web sign-in and the App check |
| `EVO_HUB_SESSION_SECRET` | api | at least 32 random characters; signs session and CSRF values and keys the hashes of worker pairing codes, which answer 503 without it |
| `EVO_HUB_PUBLIC_URL` | api | the URL browsers use, such as `https://hub.example.org` |
| `EVO_HUB_S3_ENDPOINT`, `EVO_HUB_S3_BUCKET`, `EVO_HUB_S3_ACCESS_KEY_ID`, `EVO_HUB_S3_SECRET_ACCESS_KEY` | api, worker | the blob store, all four or none; for R2 the endpoint is `https://<account id>.r2.cloudflarestorage.com` |
| `EVO_HUB_SENTRY_DSN` | api, worker | optional error reporting |
| `EVO_HUB_LOG_LEVEL` | api, worker | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |
| `EVO_HUB_BLOB_CONCURRENCY` | api | uploads one process checks and copies in the blob store at once, every commit together; default `32`, at most `256` |
| `EVO_HUB_KG_KEEP_ARTIFACTS` | api, worker | newest built graphs of each project whose artifact stays in the bucket; older ones are deleted every hour; default `3`, at least `1` |
| `EVO_HUB_RUN_LOG_DAYS` | worker | days the events of a finished run are kept before the daily pruning deletes them; default `30`, from `1` to `3650` |
| `EVO_HUB_FORWARDED_ALLOW_IPS` | api | the reverse proxies whose `X-Forwarded-For` the api believes: IP addresses or networks, comma-separated, or `*`; unset keeps uvicorn's default, the loopback addresses (or its own `FORWARDED_ALLOW_IPS`) |
| `EVO_HUB_WORKER_CPUS`, `EVO_HUB_WORKER_MEMORY` | compose | worker limits, default `2` and `4g` |
| `EVO_HUB_API_INTERNAL_URL` | web | where the web server reaches the api, default `http://evo-agents-hub-api:8080` (the api's network alias) |
| `EVO_HUB_WEB_TIME_ZONE` | web | time zone of dates rendered on the server, default `Asia/Ho_Chi_Minh` |
| `EVO_HUB_PLATFORM_NETWORK` | compose | the external network the database is on |

The server also reads `EVO_HUB_POOL_MIN_SIZE`, `EVO_HUB_POOL_MAX_SIZE` and `EVO_HUB_POOL_TIMEOUT` (the connection
pool, defaults 1, 10 and 10 seconds), and the api `EVO_HUB_RUN_LEASE_SECONDS`, how long a claim and each heartbeat of
a worker daemon lease a run for before the reaper finds it lost (default 300, from 5 to 3600; the end-to-end tests
shorten it, and it must stay well above the daemon's heartbeat of 15 seconds), and the worker
`EVO_HUB_DECISION_WAIT_SECONDS`, how long a plan run waits for its owner's answer before the reaper parks it (default
86400, a day, from 1 to 604800; the end-to-end tests shorten it); add them to the environment block of the compose
file to change them. A missing or malformed variable stops the process with a log line naming it.

The reverse proxy routes the public domain: `/v1` and `/mcp` to the api on port 8080, everything else to the web on
port 3000. `EVO_HUB_PUBLIC_URL` must be that domain, because the web sign-in callback, the `/mcp` host check and the
web terminal's Origin check all use it. The `/v1` rule also carries the web terminal's two websockets, so the proxy
must pass a websocket upgrade there (Traefik does without more configuration). A stack without a proxy serves them
through the web's `/v1` rewrite instead, since Next.js standalone forwards the upgrade to the api
(`web/e2e/terminal.spec.ts` checks it).

The api sees the proxy's address as the client's unless `EVO_HUB_FORWARDED_ALLOW_IPS` lists the proxy. The limit on
refused pairing codes counts per client address, so behind a proxy that is not listed, 10 wrong codes from anyone hold
every join for 10 minutes. List the address or network the proxy reaches the api from, as `docker inspect` shows it on
the shared network, and nothing wider: a listed address can claim any client address in `X-Forwarded-For`, and `*`
lets every client do that. The default trusts nobody new.

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
rclone copy r2:<bucket>/blobs backup:<bucket>/blobs    # any S3-capable copy tool works
```

Dump first, then copy the bucket, with a copy that never deletes from the backup (`rclone copy`, not `rclone sync`).
Blobs never change, and the only ones the hub deletes are artifacts of old graphs, so a bucket copy taken after the
dump holds every blob the dump refers to, as long as no prune ran in between: the hourly prune runs at minute 31. Skip
`uploads/` (transient) and the cache volumes.

In a live database every hash that `blobs`, `skill_versions`, `kg_builds.artifact_sha256` and `kg_ingests.log_sha256`
name has its object in the bucket, prunes included: a prune drops the references in one transaction, then deletes the
objects. `blob_deletions` lists the blobs being deleted, which are not references. A restore check that compares an
older dump with today's bucket can find artifacts pruned since the dump. When the newest build of a restored project
is one of them, its `kg_*` tools answer with an error until `evo-agents hub kg build --project P` builds the graph
again from the run logs, which are never deleted.

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
Tokens, worker pairing codes, DSN passwords, S3 keys and presigned signatures are masked before a line is written.
Set `EVO_HUB_SENTRY_DSN` to also send errors to Sentry; an event carries no request body, query string, cookie or
local variable and goes through the same masking.

**Upgrades.** Set `EVO_HUB_VERSION` to the new release, pull, and recreate the services; the api migrates on start.
Take a backup first when the release notes mention a migration.
