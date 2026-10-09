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

## The web

The web reads the API only through a client generated from its OpenAPI document (`pnpm gen:api`, below), with the
session cookie. From 0.6.0 it follows the evo-agents hub UI kit, in light and dark; `web/DESIGN.md` describes the
design and `web/README.md` how to run and test it. The pages that read a route of their own:

| Page | What it shows | Route |
| --- | --- | --- |
| Home, `/` | what waits on you, what runs and what ended lately over the projects of your grants, with your workers; a decision answered in a sheet over it, a failed step rerun from it | `GET /v1/me/overview` |
| Monitor, `/monitor` | every run in flight over the projects of your grants, and a grid of live tiles that shares out the window (up to nine within it, then four columns that scroll): each tile a run's state, worker, time, plan steps and the tail of its trace, for viewing only; `?runs=project:id,...` names the tiles, ended runs included, and without it the grid follows every run in flight | `GET /v1/me/overview`, `.../runs/{id}`, `.../stream?after=` (200 events before `last_seq`), `.../events?after=` while a stream fails |
| a run, `/p/{project}/runs/{id}` | its phases on a timeline; the Trace of its events (messages, thinking, each tool call with its argument, exit code, duration and output), the raw log and the owner's terminal as tabs; its tokens by type and the cost the agent reported; from 1280 px the session and the side column scroll on their own; a repo links to its forge page from the project's origin, and on GitHub or GitLab its branch and commit too | `.../runs/{id}`, `.../events`, `.../stream`, `GET /v1/projects/{project}` |
| Insights, `/p/{project}/insights` | the project's runs by UTC day over 7, 30 or 90 days: outcomes, failure rate, p50 and p90 run time, tokens by type, each chart with its table | `GET /v1/projects/{project}/runs/stats` |
| Administration, `/admin` | what needs a hub admin, each row opening its list filtered; the rows of every table on `/admin/diagnostics` | `GET /v1/admin/overview`, `GET /v1/admin/stats` |

Cmd K or Ctrl K, or the top bar's search field, opens a command palette: runs, plans, workers and pages to jump to,
and the actions the visitor's grants allow (Run plan, Dispatch a step, Rerun, Register worker), never one that
deletes, cancels, drains or revokes. `?` opens the list of keyboard shortcuts: G then H, I, P, R or W goes to Home,
Inbox, Plans, Runs or Workers, D opens Dispatch for a writer, / focuses the page's search. A switch in that dialog turns
the single keys off on the browser. Every page takes the full width beside the sidebar, less a 24 px gutter (16 px
under 768 px). Under 768 px tables become lists whose rows open their page, filters move into
a sheet, controls are 44 px, and an Inbox decision is a screen of its own with its answer in a bar at the foot. The
server renders the phone layout from the user agent and `Sec-CH-UA-Mobile`, so nothing swaps once the scripts run.

## The API

Every route lives under `/v1` and answers JSON. Errors have one shape, `{error, message, request_id}`, whatever went
wrong: a 404, a validation failure or an unhandled exception never answers with HTML or a traceback, and the
`request_id` is the one in the api's log line for that request.

A request carries one of three credentials:

- a machine token (`evh_...`) as `Authorization: Bearer`, which `evo-agents hub login` stores on a machine;
- a web session (`evs_...`) in the `evo_hub_session` cookie. A write made with the cookie (POST, PUT, PATCH,
  DELETE) also needs the `X-Evo-CSRF` header, whose value `GET /v1/auth/web/csrf` hands out;
- a worker token (`evw_...`) as `Authorization: Bearer`, which a worker gets once when it joins or registers. It
  works only on `/v1/worker/*`, where machine tokens and web sessions get 403, and gets 403 everywhere else under
  `/v1` (`docs/workers.md`); outside `/v1` it opens `/mcp` for the agent of a run its worker holds (below). Revoking
  it (`DELETE /v1/tokens/{id}`, or `DELETE /v1/admin/tokens/{id}`) revokes its worker too and releases the runs the
  worker holds.

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
| admin | `/v1/admin/overview`, `/v1/admin/users`, `/v1/admin/stats`, `/v1/admin/projects/{project}/grants/{login}`, `/v1/admin/audit`, `/v1/admin/tokens`, `/v1/admin/kg/prune` |
| projects | `GET /v1/projects`, `GET` and `PUT /v1/projects/{project}` |
| plans | `/v1/projects/{project}/plans`, `.../plans/{plan_id}` (`GET`, `PUT`, `PATCH`), `.../revisions`, `.../diff`, `.../complete` |
| memories | `/v1/memories` (`GET`, `PUT`), `/v1/memories/search`, `/v1/memories/{id}`, `.../revisions` |
| skills | `GET /v1/skills`, `/v1/skills/global/{name}` and `/v1/skills/projects/{project}/{name}`, each with `/versions` and `/bundle` |
| blobs | `POST /v1/blobs/uploads`, `POST /v1/blobs/commit` |
| knowledge graphs | `/v1/kg/{project}/config`, `.../runs`, `.../blobs/check`, `.../builds`, `.../tools/{tool}`, and the web's `.../graph`, `.../nodes`, `.../node`, `.../neighbourhood` |
| workers | `POST /v1/workers/pairings`, `GET /v1/workers/pairings/{id}`, `POST /v1/worker/join`, `GET` and `POST /v1/workers`, `GET /v1/workers/{id}`, `POST /v1/workers/{id}/{drain,undrain,dispatch-from,revoke}` |
| secrets | `GET /v1/secrets`, `PUT` and `DELETE /v1/secrets/{name}`, the caller's own only (`docs/credentials.md`) |
| runs | `/v1/projects/{project}/plans/{plan_id}/ready-steps`, `GET` and `POST /v1/projects/{project}/runs`, `GET .../runs/stats`, `POST /v1/projects/{project}/plan-runs`, `.../runs/{id}`, `.../events`, `.../stream`, `.../diff`, `.../messages`, `.../credentials`, `.../tool-stats`, `.../{cancel,approve,rerun,takeover,handback}`, and `GET /v1/projects/{project}/tool-stats` |
| session digests | `GET /v1/projects/{project}/digests`, `GET` and `PUT /v1/projects/{project}/digests/{session_id}` |
| curator | `GET /v1/projects/{project}/curator`, `.../curator/charter` (`GET`, `PUT`), `.../charter/revisions`, `POST .../curator/{pause,resume}`, `GET .../curator/nights`, `.../curator/figures`, `.../curator/findings`, `.../findings/{id}`, `.../curator/proposals`, `.../proposals/{id}`, `.../proposals/{id}/ledger`, `POST .../proposals/{id}/answer`, `GET .../curator/changes`, `GET .../curator/protection`, `POST .../curator/protection/{repo}/check` (`docs/curator.md`) |
| decisions | `GET /v1/projects/{project}/decisions`, `.../decisions/{id}`, `POST .../decisions/{id}/answer` |
| notifications | `GET /v1/me/notifications`, `GET /v1/me/notifications/count`, `POST /v1/me/notifications/read` |
| telegram | `GET` and `DELETE /v1/me/telegram`, `POST /v1/me/telegram/link` (a web session only), `POST /v1/telegram/webhook` (Telegram's, with its secret header), `GET /v1/admin/telegram`, `POST /v1/admin/telegram/webhook`, `DELETE /v1/admin/users/{login}/telegram` (`docs/notifications.md`) |
| overview | `GET /v1/me/overview`: counts, active, recent runs and open decisions over the projects you hold a grant on, and where each project's Curator stands, for the web's Home |
| worker protocol | `/v1/worker/{claim,heartbeat}`, `/v1/worker/runs/{id}/{state,events,inbox,uploads,blobs,plan,decisions,notices,credentials,findings,proposals,judge,verdict}`, `/v1/worker/runs/{id}/steps/{key}` |

`/mcp` speaks MCP's Streamable HTTP transport, statelessly: each POST carries one JSON-RPC message and gets one JSON
answer. It takes a machine token, or the worker token of the agent of a run (below), never a web session, and the
`Host` header must be the host of `EVO_HUB_PUBLIC_URL` or a loopback name. Runtimes reach it through
`evo-agents hub mcp`, a stdio proxy that adds the token, the session's project (`X-Evo-Project`) and its sink
(`X-Evo-Sink`, `claude-code@anthropic` by default). Its 21 tools are the seven `kg_*` tools of `evo-agents kg serve`
plus `memory_search`, `memory_get`, `memory_write`, `plan_list`, `plan_show`, `plan_step`, `skill_list`,
`hub_projects`, `run_tool_stats`, and what the Curator's review run reads: `curator_figures`, `digest_list`,
`digest_show`, `run_events` and `decision_list`; they follow the same rules as the REST routes.

The agent of a run on a worker reaches `/mcp` with its worker's token. Inside a run (`EVO_RUN_ID`, which the daemon
sets, and a token in the worker's state, `$EVO_WORKER_HOME` or `~/.evo/worker`) `evo-agents hub mcp` sends that
token with `X-Evo-Run: <run id>` instead of the machine token of `~/.evo/hub/token`, and no `X-Evo-Project`; a
worker machine needs no machine token at all. The hub opens the session only while the worker holds the run (leased,
running, interactive, verifying or waiting) and the run's owner still holds a grant on its project; any other run,
another worker's, one that ended or one in review, gets 403, and so does a worker token without `X-Evo-Run`. A
machine token with `X-Evo-Run` gets 400. The agent then acts as the run's owner, scoped to the run:

- the session's project is the run's, and an `X-Evo-Project` naming another one gets 403;
- every tool reaches the run's project alone: any other project answers as one that does not exist, `hub_projects`
  lists that one, and `skill_list` the global skills and that project's;
- the owner's role there counts as writer at most, and the level as the owner's grant allows, through the session's
  sink as for anyone;
- the agent is never a hub admin, whoever owns the run, and it neither reads nor writes its owner's personal
  memories;
- every write is audited with the worker's token, and the log line of each tool call names the run.

Once the run leaves those states, its id opens nothing, and revoking the worker ends its token.

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
evo-agents hub admin telegram-unlink alice   # unlink a member's Telegram chat
```

The audit trail and every user's tokens are on the web's admin pages (`/v1/admin/audit`, `/v1/admin/tokens`). The
web's Administration page reads `/v1/admin/overview`: members seen within 30 days, tokens expiring within 14 days or
unused for 90 (`/v1/admin/tokens?state=expiring` and `?state=unused` list them), each project's grants by role, the
bytes the blob store holds and the deletions still pending, graph builds that failed within 7 days, offline workers
and the audit rows of the last 24 hours. The rows of every table are on its Diagnostics page.

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
members (`notifications`, `notification_channels`, `notification_deliveries`), which `docs/notifications.md` describes.
Going back to 0009 deletes the plan runs with their decisions and notifications. Schema 0012 adds the night shift of the
Curator (`docs/curator.md`): `charters`, every revision of a project's charter, `schedules`, and on `runs` the schedule
and night that queued a run and its caps (`schedule_id`, `schedule_night`, `budget`), with `dispatched_via` `schedule`;
going back to 0011 drops them, and the runs a schedule queued stay, as dispatched with a machine token. Schema 0013 adds
`run_tool_stats`, the tool figures of each run that ended (`docs/workers.md`, "Tool figures"), and `session_digests`
(Memories, skills and knowledge graphs, below); going back to 0012 drops both. Schema 0014 adds the review run (a run of
kind `review`, on no plan: its `plan_id` and `plan_revision` are null, and the API shows its `plan_id` empty),
`workers.run_kinds`, `curator_figures`, `findings`, `proposals` and the notifications of kind `proposal` (The Curator's
review, below); going back to 0013 deletes the review runs and the notifications of proposals. Schema 0015 adds
`curator_briefs`, the notice `curator_brief`, `telegram_links` and `notification_deliveries.external_id`, and schema
0016 binds a Telegram chat and its link code to the web session that linked it (`docs/notifications.md`); going back to
0014 drops what the two add and deletes the briefs' notifications. Schema 0017 adds the judge run (a run of kind
`judge`), `curator_changes` and `curator_repo_checks` (The Curator's changes, below); going back to 0016 deletes the
judge runs and drops the two tables. Schema 0018 adds `curator_ledger`, the revert the hub proposes (`proposals.kind`
`revert` with `revert_of`), the reason the circuit breaker paused a schedule (`schedules.pause_reason`, with `paused_by`
null) and the notice `curator_paused` (The Curator's ledger, below); going back to 0017 drops the ledger, deletes the
revert proposals and the notices `curator_paused`, and keeps a schedule the breaker paused paused, as its owner's pause.
Schema 0019 adds `curator_changes.judge_key`, the SHA-256 of the key a judge run's claim hands its daemon (The Curator's
changes, below); going back to 0018 drops it, and a judge run in flight then takes the worker token alone again.

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
evo-agents hub run credentials 41                             # the leases the run got, never their values
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

`run credentials` lists the leases the run got (`GET /v1/projects/{project}/runs/{id}/credentials`): for each, the
secret's name or `github-app:<account>`, its provider, its target (the variable it set, or the origins it answered
for), the worker, when it was issued and when it ends, and whether it is still out, expired, or revoked and when. A
lease given back stays in the list; no value is ever in it. It belongs to the member who dispatched the run too, since
it names their secrets: another member gets 403, a hub admin included. Every refusal of the hub is printed as its
message on stderr, so `run dispatch`, `run plan` or `run rerun` pinned to a worker whose owner set it to take runs
dispatched from the web only says that a token cannot hand that worker work, and that nothing was dispatched
(`docs/credentials.md`).

The secrets those leases come from are the member's own, written once and never read back:

```sh
pbpaste | evo-agents hub secret set claude-oauth --kind env --env-var CLAUDE_CODE_OAUTH_TOKEN --project demo
evo-agents hub secret set gitlab-kb --kind git --url-prefix https://gitlab.example.org/group --project demo \
  --worker mac-mini --expires 2027-01-31                      # at a terminal: asks for the value, without echo
evo-agents hub secret list                                    # name, kind, target, projects, workers, dates
evo-agents hub secret delete gitlab-kb                        # its leases still out are revoked
```

`secret set` creates the secret or replaces it whole (`PUT /v1/secrets/{name}`). It takes no flag for the value, which
`ps` and the shell's history would see: the value comes from stdin when stdin is not a terminal, with one final line
break dropped, and otherwise it is asked for without echo. Every argument is checked before the value is read, and no
command prints the value, an error included. `--kind env` needs `--env-var`; `--kind git` needs `--url-prefix`, and
`--username` defaults to `oauth2`. `--project` names a project whose runs get it, on which you hold writer, and
`--worker` one of your workers that alone gets it; both repeat. `--expires YYYY-MM-DD` ends it at 00:00 UTC of that
day, as GitLab ends an access token on its expiry date. `secret list` reads `GET /v1/secrets` (`--json` prints it),
and `secret delete` is `DELETE /v1/secrets/{name}`. `docs/credentials.md` describes which leases a run gets of them.

The agent of a plan run asks its owner the decisions it may not take alone (`docs/notifications.md`), and the hub
tells the owner of them, and of pushes to a default branch, in notifications:

```sh
evo-agents hub decision list --state open                     # newest first; --run, --plan, --limit, --offset
evo-agents hub decision show 7                                # the question, its context, the options, the answer
evo-agents hub decision answer 7 --option postgres            # or --text "...", or both; --text - reads stdin
evo-agents hub notifications --unread                         # yours, open decisions and proposals first, then newest
evo-agents hub notifications --read all                       # or --read 12,14
```

`decision list` and `decision show` read the decisions of the plans you may read (`GET
/v1/projects/{project}/decisions`, `.../decisions/{id}`), and find the project as `run` does. `decision answer` (`POST
.../decisions/{id}/answer`) belongs to the member who dispatched the run: another member gets 403, and a decision that
is no longer open 409. The answer names an option of the decision, gives words of your own (at most 4 KiB), or both. It
goes to the run's inbox, which the worker hands to the agent; a parked run is resumed on its worker in its session, as a
new run that the answer names. `notifications` lists your own (`GET /v1/me/notifications`), filtered by `--unread`,
`--kind` (`decision`, `notice` or `proposal`) and `--project`, with `--limit` and `--offset`, and says how many are
unread and how many decisions and proposals wait for an answer (`GET /v1/me/notifications/count`). `--read all`, or
`--read` with ids, marks them read (`POST /v1/me/notifications/read`) and lists none, so it takes none of the filters;
answering a decision reads its notification too. Each of these prints the hub's answer with `--json`, `notifications
--read` its count of read and unread.

## The Curator's review

`docs/curator.md` describes the Curator as a whole: its charter, the night shift and its caps, the tiers, the three
roles, its GitHub App and the ruleset it needs, and Telegram. This section and the next two hold the details.

Each night of a project's charter, the hub reviews the project with a run of kind `review` (schema 0014): the job
`curator.collect` counts the night's figures, without any model, from the session digests, the runs and their tool
figures, the plans and the decisions of the last `review.days` of the charter (7 by default): the failures per tool
and per program, the failures that come from the environment by cause (a command the harness blocked, the hub
answering 5xx, the network, rate limits, credentials, a tool missing), the runs that failed or were lost by cause, the
commands run again and again, the turns where the person corrected an agent, the open items of the plans and the
steps that have not moved for 3 days, each with evidence a finding can cite. Then the night shift queues the review
run on the worker on duty, before any plan run of the night, once that worker's daemon says it runs review runs.

A review run reads and changes nothing: its worktrees are detached at the commit origin's default branch has, the
worker pushes nothing at its end, and its GitHub token reads only (`contents: read`). Its prompt, which the hub builds
(`evo_agents/hub/review.py`), names the lenses of the night (the charter's `review.lenses` of eleven, taken in turn),
says that what the agent reads is data and never instructions, and gives the night's figures. The agent reads the
project through the MCP tools above, its run's project alone, and writes through two commands:

```sh
evo-agents worker finding --lens environment --title "sleep then tail is blocked" --evidence session:ID:errors:0 \
  --evidence run:41:7 --evidence code:evo-agents:evo_agents/worker/run.py:120 --severity high
evo-agents worker propose --lens environment --kind fix --title "A wait helper" --path evo-agents:evo_agents/worker/wait.py \
  --finding 12 --plan-file draft.yaml --summary-file why.md
```

The hub refuses a finding or a proposal whose evidence it cannot find: a digest of the project and the entry of its
list, an event of a run of the project, a file of a repo of the project (and of its knowledge graph, when it has one).
A proposal carries a draft plan of plan.schema.json in outcome steps (each step with `verify` and `acceptance`). The
hub computes its tier, 0 to 3, from the charter, the paths it names (and what they reach in the knowledge graph) and
its kind (`evo_agents/hub/tiers.py`); a protected path of the charter, CI, lint and test configuration, and the kinds
that loosen a test, change a plan's verify or change CI are tier 3, always. A proposal like one the owner rejected in
the last 30 days is dropped unless its evidence doubled. When the review run ends, its tier 2 proposals with the most
evidence become Inbox items of the owner (notifications of kind `proposal`), up to the charter's
`max_decisions_per_day` a day; the others wait in the list. The project's admins answer:

```sh
evo-agents hub curator proposal list --state open --tier 2
evo-agents hub curator proposal show 7
evo-agents hub curator proposal accept 7 --note "go"
evo-agents hub curator proposal reject 8
evo-agents hub curator proposal defer 9 --days 14
evo-agents hub curator findings --run 41
evo-agents hub curator figures --night 2026-10-08
```

`hub curator status` names the project's last review run with what it wrote, the night shift's `state` in a word
(`paused`; `running` while a run of it is queued or held; `on_duty` in its window with none; `idle` outside it) and
how many of the project's proposals wait for an answer; `GET /v1/me/overview` says the same for each project with a
charter (`projects[].curator`). `GET .../curator/nights` lists the latest nights (14 by default, up to 90): the runs
the night shift queued for each by how they stand, their cost, the night's review run with what it wrote, and whether
the night's figures were counted. `proposal list` (`GET .../curator/proposals`) answers `counts` with its proposals:
what each state, tier and lens would list with the other filters applied. Accepting a proposal of tier 0 or 1 also
makes it a plan of the night shift (The Curator's changes, below).

The web shows the same under each project's Curator (`/p/{project}/curator`): the night now and the nights before,
the schedule and Pause or Resume, the proposals with their filters, a proposal's evidence (the run's log, the digest's
entry, the line of code on the forge) and draft plan with Accept, Defer and Reject for the project's admins, and the
charter with its revisions and, for an admin, its form. Tier 2 proposals in the Inbox open in a sheet of their own
(`/inbox?proposal=ID`), and Home says where each project's Curator stands.

**The morning brief.** At the charter's `brief_at`, in its time zone, the job `curator.brief` sends the owner of the
night shift's schedule a notice `curator_brief` (`evo_agents/hub/server/brief.py`), once a local day: the night it
reports on (the one in progress when `brief_at` falls inside the window), its runs by how they ended and what they cost
against the night's budget, its review run with what it wrote, the merges into a default branch its runs reported, the
project's runs in review that wait for the owner's approval, the decisions that wait for the owner's answer and the
proposals that wait for an admin's (with how many are in the owner's Inbox), and the worker on duty with its last
heartbeat, so a machine that stopped shows up in the morning. The brief goes out like any notification, on the web and
on every channel the owner turned on, Telegram included (`docs/notifications.md`). A brief the hub could not send
within three hours of `brief_at` waits for the next day. `curator_briefs` keeps each brief with what it said, and `hub
curator status` names the last one (`last_brief`).

## The Curator's changes

Accepting a proposal of tier 0 or 1 makes its draft plan the Curator's plan on the hub, in the same transaction, as
the admin who accepted it (schema 0017, `evo_agents/hub/server/changes.py`, `evo_agents/hub/judge.py`): the plan
`curator-<proposal>-<slug>`, in the one repo the proposal names, every step pending, on the branch
`curator/<proposal>-<slug>`, and a change of the Curator that says where it stands. A draft that names several repos,
or a repo without an origin, becomes no plan: its change stays `open` with the reason. A write of a Curator's plan that
names another repo or branch, or changes anything but the progress of its steps (`status`, `done_at`, `evidence`,
`note`) and its `status`, is 409: its what, goal, context, verify and acceptance stay as the hub made them. A plan whose
id starts with `curator-` is made this way alone (a member's put of one is 409). No member dispatches the steps of a
Curator's plan or a plan run of it (409): the night shift alone runs it.

Each night, after its review run, the night shift queues the judge run of a change waiting for one, else the Builder
of a planned change: a plan run of its plan, pinned to the worker on duty, with the night's caps. A change on GitHub
gets a Builder only once the hub checked, within 2 days, that a ruleset keeps the Curator's App off the repo's default
branch; one on GitLab only once the charter names the owner's git secret for it (`git_secret`). The Builder's GitHub
token is the Curator's App's (`docs/credentials.md`); the Builder and the Judge of a change never run at once.

When the Builder ends done with every step of its plan done, the job `curator.changes` opens its pull request on
GitHub with the workers' App (on GitLab the push opened the merge request) and reads its files for the signs of score
hacking (`judge.hack_signs`): an assertion removed, a skip or xfail added, a threshold of a test changed, a plan's
verify or a file it runs changed, CI or the configuration of lint and tests changed, a lint warning silenced, `__eq__`
overloaded, an exit in a test, a protected path of the charter touched, and the gaps a security review found: an
assertion compared with its own file's lines alone (moved to another file, or under a guard that never runs, it is
removed), an expected value changed on a line of its own, a golden, snapshot or data file of the tests changed, an
assertion's failure caught, a skip aliased or imported on its own, an exit through `getattr(os, "_exit")` and the like,
the configuration of tests and types (`norecursedirs`, mypy's `ignore_errors`, `tsconfig.json`), the Makefile,
justfile, noxfile or package script a verify command runs, and a binary or unknown file where tests, CI or
configuration live. A list of files GitHub cuts short (more than it lists, or fewer than the pull request says it
changes) is a sign too. A sign fails the change and puts its proposal at tier 3. The files also give the proposal its
tier again (`tiers.tier_of` over the paths the pull request really changes, which only ever raises it), at the pull
request and again before a merge, so a proposal of docs whose pull request changes code is tier 1 and stays open.
Otherwise the night shift queues its judge run, which reads the proposal, the diff, the plan's verify and the
project's hidden checks (`docs/workers.md`, A judge run on the machine), never the Builder's transcript, on Codex when
the project's policy declares a sink for Codex (an id `codex@...`) that clears the change's label and the worker has
it, else on Claude Code with a model other than the Builder's. The change passes only when the Judge's agent passed it,
every verify command and hidden check ran and exited 0, the worker found no sign either, and the commit judged is the
pull request's head; the paths the worker read in the diff give the proposal its tier again. The hub writes the
verdict on the pull request as a check run of the workers' App, `evo-agents Judge`, and merges the pull request with
the workers' App, at the head the Judge passed, only when the change is tier 0, tier 0 is in the charter's
`auto_merge`, the repo's ruleset still keeps the Curator off (checked again then), the pull request is open into the
default branch, its files show no sign and no protected path, and CI is green: each check the default branch's active
rulesets require (`required_status_checks`) concluded success, as a check run of the App the rule names when it names
one, every other check run ended success, neutral or skipped, and the commit statuses are success. A ruleset that
requires no check, or a head commit whose message asks CI to skip it (`[skip ci]`, `[ci skip]`, `[no ci]`,
`[skip actions]`, `[actions skip]`, `skip-checks: true`), leaves the pull request open. It waits while CI runs, up to 6
hours, and while the project's night shift is paused (by a member or its circuit breaker) it merges nothing: a judged
change waits for it. Any other change, tier 1, a GitLab merge request, or a refusal, stays open for its owner with the
reason; the morning brief lists it with the runs that wait in review.

```sh
evo-agents hub curator changes                        # what each accepted proposal became: plan, branch, PR, verdict
evo-agents hub curator protection                     # the project's repos and the last check of their ruleset
evo-agents hub curator protection --check evo-agents  # check one now, with the Curator's App; an admin of the project
```

`GET /v1/projects/{p}/curator/changes` and `GET .../curator/protection` are for readers of the project, `POST
.../curator/protection/{repo}/check` for its admins (audited as curator.protection). The hub checks again, once a day,
every ruleset it checked before, and once more each time a Builder asks for its leases: a repo whose ruleset no longer
keeps the Curator off gets no token. A judge run reads its inputs with `GET /v1/worker/runs/{id}/judge`, which no other
run reads, and posts its verdict with `POST /v1/worker/runs/{id}/verdict`, both with the run's own key in
`X-Evo-Judge-Key`: the hub makes it when the run is claimed, hands it to the daemon in the claim alone (`curator.judge_key`),
keeps its SHA-256 (`curator_changes.judge_key`, schema 0019) and forgets it with the verdict, so the worker token alone,
which code on the worker's machine can read, gets 403 there. A worker token gets 403 on the charter's routes, as on
every route of a project.

## The Curator's ledger, outcomes and circuit breaker

Each proposal has a ledger the hub only adds to (schema 0018, `evo_agents/hub/ledger.py`,
`evo_agents/hub/server/ledger.py`): one line for each thing that happened to it, never changed or deleted, each naming
who acted (`curator`, the hub's own code; `agent`, an agent of a run of the Curator, with its run; `user`, a member),
what happened, and when it applies the commit, the default branch before and after (`before_sha`, `after_sha`), the
figures, the Judge's verdict, the pull request and when it merged. Its first line (`proposed`, or `dropped` as a
repeat) holds the figures that set the proposal off: the figure of its lens in the night's figures of the review run
that wrote it (environment failures, tool failures, failed runs, corrections, open items and stuck steps, the cost) and
the entries its evidence and its findings' evidence point at (an environment cause, a cause of failed runs, a command
run again and again), with the sessions and runs the night counted. Then come the owner's answer, the plan it became,
its Builder (`built`, or `build_failed`), the pull request the hub opened, the Judge's verdict (or the hub's own when
the diff showed signs of score hacking), the merge by the hub or by hand, a change left open with why, and its outcome.
The job `curator.changes` reads a pull request left open for its owner again every hour, so a merge or a close by
hand reaches the ledger too.

`outcome_days` after the merge (the charter's, 7 by default, 1 to 90), the job `curator.outcomes` counts the same
figures again, with `curator.collect`'s own count, over the `outcome_days` after the merge, and compares them per
session or run counted: a figure is worse when its share rose by more than a quarter and it counts at least 2 (half a
dollar for the cost), better when it fell by as much. The outcome is `revert` when a figure got worse and none better,
`keep` when none got worse, and `unclear` when they are mixed, when the proposal names no figure the hub counts, or
when either span counted fewer than 3 sessions and runs. A `revert` makes the hub propose the revert itself: a
proposal of kind `revert`, tier 1 at least, whose draft plan reverts the merge commit alone (`git revert -m 1`, and a
verify that every file the merge changed is as it was before), whose evidence is what the figures after the merge hold
for the worse figures, and which names the proposal it undoes (`revert_of`). It reaches the owner's Inbox and Telegram
whatever room `max_decisions_per_day` leaves, and an accepted revert goes the way of any tier 1 change.

The circuit breaker stops a night shift that keeps going wrong: once the charter's
`circuit_breaker.max_failed_in_a_row` (2 by default) jobs of one night went wrong in a row, a run of the night shift
that ended failed or a merged change whose outcome is `revert` recorded in the night's span, the hub pauses every
schedule of the project with the reason (`paused_by` empty, `pause_reason` set), cancels what they queued, and sends
their owner the notice `curator_paused`, audited as curator.circuit. A run that ended done breaks the row; a cancelled
run, or a lost one the hub tries again, counts neither way. The night shift's job looks every minute, in the window or
not, so a night that ends on two failures still pauses. `evo-agents hub curator resume` lets it run again and starts
the count again.

```sh
evo-agents hub curator status            # a schedule the breaker paused says so, and why
evo-agents hub curator resume
```

`GET /v1/projects/{p}/curator/proposals/{id}/ledger` lists a proposal's lines, oldest first, for anyone who reads the
proposal, with when the hub counts its figures again (`outcome_due_at`). The web shows the ledger on the proposal's
page (`/p/{project}/curator/proposals/{id}`), and a revert proposal names the proposal it undoes.

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

**Session digests.** Stop also pushes the digest of the session once its transcript holds 6 messages, and again after
each turn that changed it, to the project the session's directory belongs to, decided as for memories; a directory of
no project, and the session of a run's agent, whose trace is on the hub already, push none. A digest
(`evo_agents/hub/digest.py`) counts the calls and failures of each tool (under `gen_ai.tool.name`) and of each program
Bash ran, and keeps what the person wrote, the Bash commands and the text of failed results, each cut short, with the
model, the tokens, the session's id and its directory. Before it leaves the machine every string that looks like a
secret is replaced by `***` (`evo_agents/hub/redact.py`): GitHub, Anthropic, OpenAI, AWS, Slack and Google keys and
tokens, private keys in PEM, hub, web and worker tokens, passwords in URIs, Bearer and Basic credentials, and the value
of any variable or key named as a secret, which is how a lease's value shows in a session; the machine's own hub
token is replaced wherever it appears. The push follows the write rule (the writer role and a hub sink), and the
digest carries the project's default label, so only members whose grant, and the sink they read through, clear it
see it (`GET /v1/projects/{project}/digests`). A later turn replaces the digest; only the member who pushed a session
may. When the hub does not answer, the digest waits in `~/.evo/hub/digest-state.json` and the next Stop that reaches
the hub pushes it, with up to three others that waited. `hub.prune_digests` deletes the digests not pushed for 90
days.

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
- `hub.prune_run_events`, daily at 04:13: deletes the events of runs that ended more than `EVO_HUB_RUN_LOG_DAYS` ago,
  once the tool figures of each such run are written from them (a run writes them when it ends; this covers the runs
  that ended before schema 0013).
- `hub.fire_schedules`, every minute: the night shift of each project with a charter queues its next run inside
  the charter's window and within the night's budget, pinned to the charter's worker and dispatched as that worker's
  owner: the night's review run first, then the judge run of a change of the Curator that waits for one, the
  Builder of a change it planned, and plan runs of the charter's `night_plans`; and it cancels the runs it queued
  that are still queued once the window ends or the project is paused (`evo-agents hub curator pause`). Its circuit breaker pauses a project's
  night shift once `max_failed_in_a_row` jobs of a night in a row failed or were reverted.
- `curator.collect`, every minute: inside each charter's window, counts the night's figures of the project once,
  without any model, and queues the night's review run; and opens again the proposals deferred until a moment that
  has passed (The Curator's review, above).
- `curator.brief`, every minute: at each charter's `brief_at`, in its time zone, sends the owner of the project's
  schedule the morning brief of the night (a notice `curator_brief`), once a day: the night's runs and cost, its
  review run, merges, runs waiting for approval, open decisions and proposals, and the last heartbeat of the worker on
  duty (`evo_agents.hub.server.brief`).
- `curator.changes`, every minute: the Curator's changes move on: their pull requests opened and read for signs of
  score hacking, the Judge's check runs written, the tier 0 ones merged when everything allows it and the others left
  open for their owner, read again hourly for a merge or a close by hand; and the rulesets checked again once a day
  (The Curator's changes, above).
- `curator.outcomes`, every 10 minutes: the figures of each change of the Curator merged the charter's `outcome_days`
  ago counted again, its outcome (keep, revert or unclear) added to its proposal's ledger, and a revert proposed when
  they got worse (The Curator's ledger, outcomes and circuit breaker, above).
- `hub.prune_digests`, daily at 04:23: deletes the session digests not pushed for 90 days.
- `hub.cleanup_uploads`, hourly: removes uploads nobody committed within 24 hours.
- `hub.prune_jobs`, daily: removes finished jobs older than 14 days.

`evo-agents hub worker --data-dir /cache --concurrency 2` runs two jobs at a time (default 1). On SIGTERM it takes no
new job and gives running ones 25 seconds, so give its container a stop grace period above that (the compose file
uses 40 s). It exits 1 when it loses Postgres, which lets the restart policy bring it back. Graph builds use CPU and
memory, so the compose file caps the worker with `EVO_HUB_WORKER_CPUS` and `EVO_HUB_WORKER_MEMORY`. The worker
refuses to start without a blob store, since builds read from it and write to it.

## Data access

The api and the worker each hold one psycopg 3 connection pool and a SQLAlchemy 2.1 engine on it
(`evo_agents/hub/db.py`). The engine keeps no connection of its own (`NullPool`): it takes each one from the pool
(`async_creator=pool.getconn`), and the pool is made with `close_returns=True`, so a connection the engine closes goes
back to it. The pool's size, timeout and health check govern both. To `NullPool` each checkout is a new connection, so
the psycopg dialect's connect hook, which adds a handler that logs the server's notices, runs on every checkout of the
same pooled connection; `make_engine` takes the handler off as the engine gives the connection back, so it does not pile
up (`tests/hub/test_db_bridge.py`). The lifespan opens the pool, then the engine, and disposes of the engine before it
closes the pool. The worker's jobs reach the engine through `HubContext`.

A request takes one `AsyncConnection` with `async with request.app.state.engine.begin() as conn:`; its transaction
commits when the block ends cleanly and rolls back on any exception, an `HTTPException` included. A savepoint is
`async with conn.begin_nested():`. Code that must commit part way (the migration lock, the dry run of the kg
retention) uses `engine.connect()` and commits or rolls back itself. Helpers take that connection as their first
argument, so the queries of one request share its transaction.

Queries are SQLAlchemy Core (`select()`, `insert()`, `update()`, `delete()`, `func`, `case`, CTEs, and
`sqlalchemy.dialects.postgresql.insert` for `ON CONFLICT`) on the tables of `evo_agents/hub/tables.py`, imported as
a module (`from evo_agents.hub import tables`, then `tables.runs.c.state`). There is no ORM: no mapped classes, no
session, no lazy loading. A row maps to a Pydantic model by column name, `Model(**row._mapping)` with the columns
labelled as the model's fields, never by position. A JSONB column takes and returns plain dicts and lists, and an
error of the database arrives as SQLAlchemy's wrapper (`sqlalchemy.exc.IntegrityError` and so on) with the psycopg
error, which names the SQLSTATE, in `.orig`. `print(statement.compile(dialect=postgresql.psycopg.dialect()))` shows
the SQL a statement sends.

A statement every request of a busy route runs (the token check, a project's access, the reads of GET
/v1/me/overview, of a project's runs and plans, and of memories) is built once, at import or with `functools.cache`
once per shape of its filters, and runs with bind parameters: `await conn.execute(STATEMENT, {"name": value})`.
Building a statement and its cache key on each call costs more than its round trip to Postgres. A bind parameter of
an INSERT or UPDATE, or of one inside a CTE, never has the name of a column of its table: the execution would set
that column too (`tests/hub/test_prebuilt.py`). `db.one_of(column, values)`, or `one_of(column, name=...)` for a list
bound at execution, is `column = ANY(array)`: an `in_()` list is expanded into one parameter per value at each
execution.

`driver(conn)` returns the psycopg connection under an engine connection, in the same transaction. Only `jobs.py`
takes it, to hand it to procrastinate, so a job deferred with `connection=conn` exists only if the caller's
transaction commits; a defer refused because one job waits already rolls back to a savepoint and the transaction
goes on.

`tests/test_no_raw_sql.py` reads `evo_agents/hub` and `tests/hub` and fails, naming the file and line, on a string
that holds SQL, on `psycopg.sql`, SQLAlchemy's `text` or `exec_driver_sql`, on `driver()` outside `jobs.py`, and on
any other reach for the raw psycopg connection outside `db.py`. Three places keep SQL for good, each for a reason no
query builder changes:

- the migrations 0001 to 0011, which have run as they are and are the record of how those databases were built;
- the `LISTEN` of `evo_agents/hub/server/listen.py`, which keeps a psycopg connection of its own in autocommit,
  because SQLAlchemy has no construct for `LISTEN`;
- `CREATE` and `DROP` of a test's database and role in `tests/hub/pg.py`, run by the superuser outside any hub
  database, where SQLAlchemy has no construct either.

When the test reports a finding, write the statement with Core on `tables.py`: a table the hub does not own (a
`pg_*` catalog, `procrastinate_jobs`) is described where it is read with a lightweight `sqlalchemy.table()`. A test
seeds and reads the database the same way, through `tests.hub.live.sql(db, statement)`, which runs a Core statement
on a sync engine as the database's owner and raises the driver's error. A change that only raw SQL can make adds
its place to the test's allowed lists (`ALLOWED_SQL`, `ALLOWED_PSYCOPG_SQL` or `ALLOWED_DRIVER`) in the same pull
request, with the reason in a comment next to it. Where SQLAlchemy lacks a keyword, a small construct of
`sqlalchemy.ext.compiler` adds it around a compiled Core statement, as `decisions.py` does for the `OVERRIDING SYSTEM
VALUE` of the insert that resumes a parked plan run with an id reserved beforehand, and `tests/hub/test_run_tables.py`
for `EXPLAIN` and `ANALYZE`.

A new migration starts from `tables.py`: change the table there, then let Alembic compare the metadata with a
scratch database that `evo-agents hub migrate` brought to head and write the revision, from a Python shell:

```python
from alembic import command
from sqlalchemy import create_engine
from evo_agents.hub.migrate import alembic_config

config = alembic_config()
with create_engine("postgresql+psycopg://user:password@localhost/db").connect() as conn:
    config.attributes["connection"] = conn
    command.revision(config, message="what changes", autogenerate=True, rev_id="0012")
```

Read the generated `versions/0012_*.py` before keeping it: autogenerate misses renames, compares neither CHECK
constraints nor triggers, and orders operations by table. Name the file `NNNN_name.py`, add what it cannot see with
Alembic's operations (`op.create_check_constraint(...)`, `op.create_index(...)`), and give the downgrade the same
care, then run `ruff format` on it. `tests/hub/test_schema_metadata.py` checks that the migrated schema and
`tables.py` agree.

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
| `EVO_HUB_VERSION` | compose | image tag, a released version such as `0.8.0` |
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
| `EVO_HUB_SECRETS_KEY` | api, worker | 32 random bytes in base64url that seal the credentials of worker runs (`docs/credentials.md`); without it writing a secret answers 503 and runs get no lease. It is not in the database or its dumps |
| `EVO_HUB_GITHUB_APP_ID`, `EVO_HUB_GITHUB_APP_PRIVATE_KEY` | api, worker | the GitHub App that makes each run a token for its repos only: its ID or client ID, and its private key in PEM, where `\n` may stand for each line break; both or neither |
| `EVO_HUB_CURATOR_APP_ID`, `EVO_HUB_CURATOR_APP_PRIVATE_KEY` | api, worker | the Curator's own GitHub App, evo-agents-curator, as the two above: every run of the Curator gets its GitHub token from it alone, and the hub checks with it that each repo's ruleset keeps it off the default branch; both or neither, and without them no run of the Curator gets a GitHub token (`docs/curator.md`) |
| `EVO_HUB_TELEGRAM_BOT_TOKEN`, `EVO_HUB_TELEGRAM_WEBHOOK_SECRET` | api, worker | the hub's Telegram bot, as @BotFather gives its token, and the secret Telegram sends back with each update (1 to 256 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`); without either the Telegram channel is off and the hub runs on (`docs/notifications.md`). `evo-agents hub admin telegram --set-webhook` then points the bot at `EVO_HUB_PUBLIC_URL/v1/telegram/webhook` |
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

The dump holds the secrets of worker runs sealed, and never `EVO_HUB_SECRETS_KEY`. Keep the key in the operator's
secret store, apart from the dumps: a dump restored without it keeps every secret sealed, so their owners set them
again, and a dump and the key together open every secret.

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
