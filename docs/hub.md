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
| Home, `/` | what waits on you, what runs and what ended lately over the projects of your grants, with your workers; a decision answered in a sheet over it, a failed step rerun from it; the author runs that wait for your reply in their chat (one that waits on a decision of its own stands in Needs you through the decision alone, not again there), and a decision of an author run without a plan named "Author run #N" with the run's title, never a link to a plan | `GET /v1/me/overview` |
| Monitor, `/monitor` | every run in flight over the projects of your grants, and a grid of live tiles that shares out the window (up to nine within it, then four columns that scroll): each tile a run's state, worker, time, plan steps and the tail of its trace, for viewing only; `?runs=project:id,...` names the tiles, ended runs included, and without it the grid follows every run in flight | `GET /v1/me/overview`, `.../runs/{id}`, `.../stream?after=` (200 events before `last_seq`), `.../events?after=` while a stream fails |
| a run, `/p/{project}/runs/{id}` | its phases on a timeline; the Trace of its events (messages, thinking, each tool call with its argument, exit code, duration and output), the raw log and the owner's terminal as tabs; its tokens by type and the cost the agent reported; for an author run a Chat tab first among them (`?tab=chat`): the agent's and the owner's messages, whose turn it is, a reply box, End chat and a link to the plan it wrote or revises, and each decision the agent asked as a DecisionCard right after the agent's message that ended the turn which asked it, in the order asked: the owner picks an option with the mouse (the recommended one chosen already), adds words if they like and sends each card, an answered card folds to its answer (which the chat does not repeat as a message), the label of whose turn it is says how many decisions wait, another member reads the cards without a form, and "Asked you" in the Trace opens the card in this tab; for a plan run with a session for each step a Step sessions card, each step's agent session with its id to copy, the largest context it read and its cache reads; from 1280 px the session and the side column scroll on their own; a repo links to its forge page from the project's origin, and on GitHub or GitLab its branch and commit too | `.../runs/{id}`, `.../events`, `.../stream`, `.../chat`, `.../messages`, `.../finish`, `GET /v1/projects/{project}` |
| a project's plans, `/p/{project}/plans`, and a plan, `.../plans/{id}` | a plan's steps as Board (by status, the default), List (in plan order) or Graph (`?view=graph`): the graph of their dependencies top to bottom, a step under every step it waits for, lines solid when the step waited for is done and dashed while it is not, Ready steps tagged, a dependency implied by another one not drawn; a step selected (`&step=11`) dims what it neither waits for nor unblocks and opens a read-only inspector beside the graph from 1024 px and in a bottom sheet below, with its dependencies, what waits for it, What to do, Verify, Evidence, its acceptance criteria and a link to the step's page; the view is kept in the URL and on the browser for the next plan; New plan and Revise with agent, for a writer: a dialog takes the request, the worker and the model as Run plan does, and queues an author run (Workers and runs, below) | `POST /v1/projects/{project}/author-runs` |
| Insights, `/p/{project}/insights` | the project's runs by UTC day over 7, 30 or 90 days: outcomes, failure rate, p50 and p90 run time, tokens by type, each chart with its table | `GET /v1/projects/{project}/runs/stats` |
| Administration, `/admin` | what needs a hub admin, each row opening its list filtered; the rows of every table on `/admin/diagnostics` | `GET /v1/admin/overview`, `GET /v1/admin/stats` |
| Plugins, `/admin/plugins` and `/admin/plugins/{name}`; a project's plugins, `/p/{project}/plugins` and `.../plugins/{name}`; a plugin's page, `/p/{project}/x/{plugin}/{page}` | for a hub admin, the store (versions, an upgrade waiting, Install, Upgrade, Remove), the trusted publishers, a plugin's backend and the hub's layer of its config; for a project's members, its plugins, enabled through a consent that lists what each asks for, and the form of the project's layer drawn from the plugin's config schema (a secret never shown again); the pages enabled plugins add to the project's menu, an isolated plugin's framed in a sandbox, a core plugin's native (`docs/plugins.md`, section Pages) | `GET /v1/plugins`, `GET /v1/plugin-publishers`, `GET /v1/projects/{project}/plugins`, `.../config`, `.../config/effective` |

Cmd K or Ctrl K, or the top bar's search field, opens a command palette: runs, plans, workers and pages to jump to,
and the actions the visitor's grants allow (Run plan, New plan, Revise with agent, Dispatch a step, Rerun, Register
worker), never one that deletes, cancels, drains or revokes. `?` opens the list of keyboard shortcuts: G then H, I, P, R or W goes to Home,
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
`/v1/auth/web/login`, `/v1/auth/web/callback`), a worker's join with a pairing code (`/v1/worker/join`), Telegram's
webhook (its secret header is the credential), the hub's branding, which the sign-in page shows (`GET /v1/branding`,
`/v1/branding/logo`, `/v1/branding/favicon`), and the files of a plugin's page under `/v1/plugin-ui/`, whose ticket
in the path is the credential, answer without one. A route added later needs a credential unless it is added to
`PUBLIC_PATHS` or `PUBLIC_PREFIXES` in `evo_agents/hub/server/security.py`. Postgres keeps only the SHA-256 of a token. A token nobody uses for 90 days
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
| admin | `/v1/admin/overview`, `/v1/admin/users`, `/v1/admin/stats`, `/v1/admin/projects/{project}/grants/{login}`, `/v1/admin/audit`, `/v1/admin/tokens`, `/v1/admin/kg/prune`, `/v1/admin/kg/scrub`, `/v1/admin/kg/scrub/purge` |
| projects | `GET /v1/projects`, `GET` and `PUT /v1/projects/{project}` |
| plans | `/v1/projects/{project}/plans`, `.../plans/{plan_id}` (`GET`, `PUT`, `PATCH`), `.../revisions`, `.../diff`, `.../handoff`, `.../complete` |
| memories | `/v1/memories` (`GET`, `PUT`), `/v1/memories/search`, `/v1/memories/{id}`, `.../revisions` |
| skills | `GET /v1/skills`, `/v1/skills/global/{name}` and `/v1/skills/projects/{project}/{name}`, each with `/versions` and `/bundle` |
| blobs | `POST /v1/blobs/uploads`, `POST /v1/blobs/commit` |
| plugins | `GET /v1/plugins`, `GET /v1/plugins/{name}`, `POST /v1/plugins/{name}/{versions,install,upgrade,remove}`, `GET` and `POST /v1/plugins/{name}/config`, `.../config/versions`, `.../config/rollback`, for hub admins only; `GET /v1/projects/{project}/plugins`, `.../plugins/{name}`, `POST .../plugins/{name}/{enable,disable}`, `GET` and `POST .../plugins/{name}/config`, `.../config/versions`, `.../config/rollback`, `.../config/effective`, `GET /v1/projects/{project}/runs/{id}/plugins`; `GET /v1/plugin-publishers` for hub admins; `GET /v1/plugin-ui/<plugin>/<project>/<ticket>/<file>`, public, the files of a plugin's page under its ticket (section Plugins) |
| knowledge graphs | `/v1/kg/{project}/config`, `.../runs`, `.../blobs/check`, `.../builds`, `.../tools/{tool}`, and the web's `.../graph`, `.../nodes`, `.../node`, `.../neighbourhood` |
| workers | `POST /v1/workers/pairings`, `GET /v1/workers/pairings/{id}`, `POST /v1/worker/join`, `GET` and `POST /v1/workers`, `GET` and `PATCH /v1/workers/{id}`, `POST /v1/workers/{id}/{drain,undrain,dispatch-from,revoke}` |
| secrets | `GET /v1/secrets`, `PUT` and `DELETE /v1/secrets/{name}`, the caller's own only (`docs/credentials.md`) |
| accounts | `GET /v1/accounts`, `GET` and `PATCH /v1/accounts/{name}`, `GET /v1/account-policies`, `GET`, `PUT` and `DELETE /v1/account-policies/{project}`, the caller's own only, never a value (`docs/credentials.md`, section "Accounts") |
| runs | `/v1/projects/{project}/plans/{plan_id}/ready-steps`, `GET` and `POST /v1/projects/{project}/runs`, `GET .../runs/stats`, `POST /v1/projects/{project}/plan-runs`, `POST /v1/projects/{project}/author-runs`, `.../runs/{id}`, `.../events`, `.../stream`, `.../diff`, `.../messages`, `.../chat`, `.../credentials`, `.../accounts`, `.../tool-stats`, `.../{cancel,approve,rerun,takeover,handback,finish}`, and `GET /v1/projects/{project}/tool-stats` |
| session digests | `GET /v1/projects/{project}/digests`, `GET` and `PUT /v1/projects/{project}/digests/{session_id}` |
| curator | `GET /v1/projects/{project}/curator`, `.../curator/charter` (`GET`, `PUT`), `.../charter/revisions`, `POST .../curator/{pause,resume}`, `GET .../curator/nights`, `.../curator/figures`, `.../curator/findings`, `.../findings/{id}`, `.../curator/proposals`, `.../proposals/{id}`, `.../proposals/{id}/ledger`, `POST .../proposals/{id}/answer`, `POST .../proposals/{id}/plan`, `GET .../curator/changes`, `GET .../curator/protection`, `POST .../curator/protection/{repo}/check` (`docs/curator.md`) |
| decisions | `GET /v1/projects/{project}/decisions`, `.../decisions/{id}`, `POST .../decisions/{id}/answer` |
| notifications | `GET /v1/me/notifications`, `GET /v1/me/notifications/count`, `POST /v1/me/notifications/read` |
| chats | `GET` and `POST /v1/chats`, `GET /v1/chats/{id}`, `PUT` and `DELETE .../people/{login}` and `.../agents/{handle}`, `GET` and `POST .../messages`, `POST` and `DELETE .../messages/{message}/reactions`, `POST .../read`, `GET .../events`, `GET .../stream` (`docs/hub.md`, "Group chats") |
| telegram | `GET` and `DELETE /v1/me/telegram`, `POST /v1/me/telegram/link` (a web session only), `POST /v1/telegram/webhook` (Telegram's, with its secret header), `GET /v1/admin/telegram`, `POST /v1/admin/telegram/webhook`, `DELETE /v1/admin/users/{login}/telegram` (the core plugin telegram, `docs/notifications.md`) |
| plugins' routes | `/v1/x/{plugin}/{project}/...`: the routes a plugin contributes, in a project that enabled it, else 404 (`docs/plugins.md`, section Extension points) |
| builder | `GET /v1/x/builder/{project}/proposals`, `.../proposals/{id}`, `POST .../proposals/{id}/answer` and `.../rollback`: the proposals of @builder, which an admin decides (`docs/plugins.md`, section Builder) |
| branding | `GET /v1/branding`, `/v1/branding/logo`, `/v1/branding/favicon`, public; `GET` and `POST /v1/admin/branding`, `.../versions`, `.../rollback`, `PUT /v1/admin/branding/{logo,favicon}`, for hub admins (section Branding and features) |
| features | `GET /v1/features`; `GET` and `POST /v1/admin/features` and `/v1/admin/projects/{project}/features`, each with `/versions` and `/rollback`, for hub admins (section Branding and features) |
| overview | `GET /v1/me/overview`: counts, active, recent runs and open decisions over the projects you hold a grant on, and where each project's Curator stands, for the web's Home |
| worker protocol | `/v1/worker/{claim,heartbeat}`, `/v1/worker/runs/{id}/{state,events,inbox,uploads,blobs,plan,chat,decisions,notices,credentials,rate-limit,findings,proposals,judge,verdict}` (`plan` is `GET`, and `PUT` for an author run), `/v1/worker/runs/{id}/steps/{key}` |

`/mcp` speaks MCP's Streamable HTTP transport, statelessly: each POST carries one JSON-RPC message and gets one JSON
answer. It takes a machine token, or the worker token of the agent of a run (below), never a web session, and the
`Host` header must be the host of `EVO_HUB_PUBLIC_URL` or a loopback name. Runtimes reach it through
`evo-agents hub mcp`, a stdio proxy that adds the token, the session's project (`X-Evo-Project`) and its sink
(`X-Evo-Sink`, `claude-code@anthropic` by default). Its 23 tools are the seven `kg_*` tools of `evo-agents kg serve`
plus `memory_search`, `memory_get`, `memory_write`, `plan_list`, `plan_show`, `plan_handoff`, `plan_step`,
`ask_member` (a question of a local session, `docs/notifications.md`), `skill_list`, `hub_projects`, `run_tool_stats`, and what the Curator's review run reads: `curator_figures`,
`digest_list`, `digest_show`, `run_events` and `decision_list`; they follow the same rules as the REST routes.
`tools/list` shows the agent of a run only the tools it may call: a judge run the `kg_*` tools and `hub_projects`, any
other run all but `ask_member`, which only a local session can call. In a Claude Code session that has the evo-kg
plugin as well (its `enabledPlugins` and the installed plugins of Claude Code's configuration say so), the proxy drops
the `kg_*` tools from the list and the graph's paragraph from the instructions, those of `initialize` and those of
`server/discover`, where a client of MCP 2026-07-28 such as Claude Code 2.1.296 reads them, so the session sees each
`kg_*` tool and that paragraph once, from evo-kg; without evo-kg it serves them from the hub's graph. While the hub
does not answer, the proxy answers `initialize`, `server/discover`, `tools/list` and every tool call itself, the same
way. Either way the proxy joins the labels of every
`kg_*` result the hub answers into the session label of `~/.evo/kg/sessions/`, as evo-kg's PostToolUse hook does, a
call to a tool the list hid included; when that fails, the answer still goes back and stderr gets one line naming the
exception's type. A label record that exists but cannot be read is left as it is, never started again from one result.

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

### Paging a list

Five lists answer a bare JSON array and page only when asked: `GET /v1/projects`, `/v1/projects/{project}/plans`,
`/v1/skills`, `/v1/workers` and `/v1/admin/users`. They take `limit` (1 to 1,000) and `offset` (0 to 1,000,000),
each optional. Without either the answer is the whole list, as before, so the command line, `hub plan export` and the
web read what they always read. With them it is that slice of the list in the list's own order, and the body stays an
array. Either way the `X-Total-Count` header holds the length of the whole list, so a client asks again with
`offset` moved on until `offset` plus what it got reaches that count; an `offset` past the end answers `[]` with the
count. A value out of range is a 422.

The count is of what the caller may see. The label rule filters plans after the database query, so a page of plans
is cut from the plans it lets through, and a member whose grant stops at `public` counts only those. The lists that
carry their own `total` in the body (runs, decisions, notifications, Curator proposals and findings, digests) page as
they did.

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

One machine can be signed in to two hubs. `EVO_HUB_HOME` names the directory of a second login, and every command,
hook and the MCP proxy then read the token, the hub URL and their state files (`hook-state.json`, `memory-state.json`,
`digest-state.json`, `local/`, `backups/`) from there instead of `~/.evo/hub`. A harness whose project lives on the
other hub sets it in the `env` of its `.claude/settings.json`, so only the sessions of that harness go there:

```json
{ "env": { "EVO_HUB_HOME": "~/.evo/hub-lumen" } }
```

```sh
EVO_HUB_HOME=~/.evo/hub-lumen evo-agents hub login --url https://hub.other.example
```

A leading `~/` is expanded, since Claude Code passes the value as written; a blank value counts as unset, and a
relative path is refused, because hooks start in whatever directory the session is in. The worker doctor still checks
`~/.evo/hub/token` only.

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
and the audit rows of the last 24 hours. The rows of every table are on its Diagnostics page. The trail keeps
`EVO_HUB_AUDIT_DAYS` days of rows (365 by default, at least 30): the worker's daily `hub.prune_audit` deletes older ones.

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

## Agent profiles

An agent profile says who an agent of a project is and what its runs may use (`evo_agents.hub.profiles`). It belongs
to a project: the project's admins create, edit and delete its profiles, and every member reads them, deleted ones
included, so an old message keeps its author's name and avatar. A writer or a reader who changes one gets 403; someone
without a grant gets the project's 404.

A profile has a handle, unique on the hub (deleted profiles' included) and never a project's name, written
`[a-z0-9-]`; a name shown for it (1 to 80 characters); a description (at most 1000); instructions, added to the system
prompt of its runs (at most 8000); a colour (`#rrggbb`, one picked by the handle when none is given); an avatar or
none; and eight axes of permissions. The handle never changes. Every edit that changes something, the avatar
included, raises its version and keeps the profile as that version had it, which `GET .../agents/{handle}/versions`
lists. A deleted profile takes no new run. Field names follow an A2A Agent Card where they can: `name`,
`description`, and `icon_url` for the avatar.

Every project has a default profile, `@<project>`, whose handle is the project's name: the registration of a project
creates it, and migration 0030 created one for every project before it. It allows everything and has no instructions,
always (422 otherwise), so a run whose dispatch names no profile, which goes under it, runs as runs ran before
profiles; its name, description, colour and avatar may change, and it is never deleted. A project cannot be registered
under the handle of another project's agent (409), since `@<name>` would name both.

Each axis is `{"mode": "all"}` (the default) or `{"mode": "allowlist", "allow": [...]}`. An allowlist never grows by
itself when a secret, a skill or a worker is added, and an empty one allows nothing. A profile only narrows what the
member who dispatches a run has (their workers, their secrets, the projects they write to); it never widens it.

| Axis | An allowlist names | Enforced |
| --- | --- | --- |
| `credential` | secret names, and `github-app` for a token of the hub's GitHub App | the leases of the run ([credentials.md](credentials.md)) |
| `tool` | the tool words below, and `mcp:<server>` | the worker's adapter, in the agent session |
| `skill` | skill names, global or of the project (ignoring case) | the dispatch of an author run (`create-exec-plan`), and the worker's adapter |
| `worker` | worker names, or `label:<label>` (ignoring case) | the dispatch (a pinned worker, or one of the dispatcher's at least) and the claim |
| `project` | the other projects whose runs the profile may take; its own always | the dispatch |
| `model` | model names, as a runtime names them | the dispatch (none named: the first allowed), and the worker's adapter |
| `runtime` | `claude-code`, `opencode`, `codex`, and `<plugin>/<name>` of a plugin's runtime | the dispatch, and the claim of a run asking for `any` (which takes a runtime of the core) |
| `environment` | where the agent runs, in order of preference: `host` (the worker's own machine) and `<plugin>/<name>` of a plugin's environment, a container | the dispatch (one the project registers), the claim (the first the worker takes) and the worker's preflight |

The tool axis speaks runtime-neutral words, which each runtime's adapter turns into its own tools: `read` (read files
and search them), `edit` (create and change files), `bash` (run shell commands), `web` (fetch web pages and search the
web), `agent` (start subagents), and `mcp:<server>` (the tools of one MCP server). Tools that only keep the agent's own
notes, such as a to-do list, are always there. A runtime that cannot turn off a word a profile leaves out fails the run
before its agent starts.

The environment axis is where its runs' agent runs. `all`, the default, runs it on the worker's host, as every run ran
before the axis; an allowlist names `host` and the environments of plugins (`docs/plugins.md`, Environments) in order
of preference, so `["sandbox/box", "host"]` takes the container where the worker can run it and the host elsewhere,
and `["sandbox/box"]` waits for a worker that can. Schema 0040 gave every profile the axis with mode `all` and made a
default profile's eight axes, all, the rule; a profile version written before it names seven axes, and the hub reads
the one it does not name as `all`.

A profile may also attach agent packs of plugins its project enabled (`docs/plugins.md`, Agent packs): `packs`, at
most 16, each `{"plugin", "name", "version"}` and once, pinned at a version of its plugin the hub holds as a bundle,
whose manifest declares the pack and that runs on this release; a pin sent without `version` takes the version
installed now. A plugin that is not enabled in the project or is turned off, a version the hub does not hold, one of
the hub's image (it has no bundle) or one that declares no such pack is a 422 that says which and that nothing
changed. The pin holds when the plugin moves on: only an edit moves it, and an edit sends the packs whole, as a new
version of the profile. `GET /v1/projects/{p}/agent-packs` lists what a profile of the project may attach, each with
the version installed now and every version that qualifies, the newest first. The default profile attaches none.

A pack a profile attaches counts as named on its skill and tool axes: its skills pass the skill axis and its MCP
servers the tool axis (`mcp:<server>`), since pinning the pack at a version is the admin's consent to what that
version holds. The axes still limit everything else (an allowlist of tools that leaves out `bash` keeps it out for a
pack's skill too), and a pack widens no other axis. Schema 0041 adds `packs` to each profile, `[]` for every profile
before it and always for a default one; each version written since keeps them in its body, and a version written
before it attaches none. Going back to 0040 drops them: a profile that attached packs runs without them.

### Runs under a profile

`POST /v1/projects/{p}/runs`, `.../plan-runs` and `.../author-runs` take `profile`, a handle (`--agent` on `hub run
dispatch`, `plan` and `author`); without it the run goes under the project's default. The dispatch is refused, saying
which allowlist leaves what out and that nothing was dispatched, when:

- the handle names no profile the caller sees (404), or one of another project where the caller is not a writer (403);
- the profile was deleted, or belongs to another project whose `project` axis leaves this one out (422);
- the runtime asked for is not allowed, or none is, or a pinned worker reports none that is (422);
- the model asked for is not allowed, or the axis allows none (422); without a model, the run takes the first allowed;
- the pinned worker is not allowed by name or label, or, unpinned, no worker of the caller's that serves the project
  is (422);
- the account picked by hand is not in the credential allowlist (422), or an author run's profile leaves out the skill
  `create-exec-plan` (422);
- the pinned worker's daemon does not say it applies profiles (feature `agent_profiles`) and the profile limits
  tools, skills or models or has instructions (409, as for any worker that could never claim the run);
- the environment axis names no environment the project registers, or allows none (422); a pinned worker whose
  daemon does not say it runs environments (feature `environments`), or whose owner has not accepted the plugin of
  any environment the profile allows, and the axis does not allow `host` (409).
- the pinned worker's daemon does not say it builds agent packs (feature `agent_packs`), or its owner has not
  accepted the plugin of each pack the profile attaches, in the worker's plugins (409).

The run records `profile_id` and `profile_version`, and `Run` shows `profile` (the handle), `profile_id` and
`profile_version`. The hub applies that version's rules to the run: a worker claims it only when the worker axis names
it or one of its labels, the runtime axis one of its runtimes, the environment axis `host` or a plugin's environment
the worker takes, and, for a profile that needs it, its daemon lists `agent_profiles` in its heartbeat's `features`; a
run asking for `any` runtime takes the first of the worker's the profile allows. The claim records the environment it
picked in the run (`environment`: `<plugin>/<name>`, null for the host) and hands its declaration
(`RunSpec.environment`). The claim carries the profile at that version (`RunSpec.profile`: handle, version, name, instructions,
permissions) for the worker to apply. A run whose profile attaches packs goes only to a worker whose daemon lists
`agent_packs` and whose owner accepted each pack's plugin; the claim hands each pack at the version the profile pins
(`RunSpec.packs`: `pack`, `plugin`, `name`, `version`, and the `url` of a presigned GET of that version's bundle
with its `sha256`, `size` and `expires_at`). A run whose pack's plugin is no longer enabled in its project, whose
pinned version the hub no longer holds, or that the hub cannot hand out for want of a blob store fails at the
claim, saying why. A rerun goes under the profile of the run it reruns, at its version now, checked
again; the next attempt of a lost run and a run that resumes a parked one keep the profile and version. A run of the
night shift goes under the project's default. A run from before 0030 has no profile and runs as before.

The audit row of a dispatch, a plan run, an author run or a rerun ends with `profile=@<handle>:v<version>`, and so does
each `credential.lease` row, unless the profile is the project's default: a row that names none went under it. Each
lease row records the profile and version it was handed out under.

### Routes and commands

| Route | Command | Who |
| --- | --- | --- |
| `GET /v1/projects/{p}/agents` (`?deleted=true` adds the deleted) | `hub agent list [--deleted]` | a member |
| `GET .../agents/{handle}` | `hub agent show HANDLE` | a member |
| `GET .../agents/{handle}/versions`, every version whole, the latest first | | a member |
| `POST .../agents` | `hub agent create HANDLE --name NAME [...]` | an admin of the project |
| `PATCH .../agents/{handle}` (`if_version`: 409 when it moved on) | `hub agent edit HANDLE [...]` | an admin of the project |
| `DELETE .../agents/{handle}` | `hub agent delete HANDLE` | an admin of the project |
| `PUT` and `DELETE .../agents/{handle}/avatar` | `--avatar PATH`, `--no-avatar` | an admin of the project |
| `GET .../agents/{handle}/avatar` | | a member |
| `GET .../agent-packs`, the agent packs a profile of the project may attach | | a member |

`--allow AXIS:ITEM` (repeatable) makes an axis an allowlist of the items given for it, `--none AXIS` an allowlist of
nothing, and on `edit` `--all AXIS` allows everything again; `edit` sends the other axes as it read them, with
`if_version`. `--pack PLUGIN/NAME[@VERSION]` (repeatable) attaches the agent packs given, in that order, each at
VERSION or the version installed now; on `edit` they replace the packs the profile attached, and `--no-packs` drops
them. For example:

```sh
evo-agents hub agent create reviewer --name Reviewer --project evo-agents --allow tool:read --allow tool:mcp:github \
  --allow credential:github-app --allow model:claude-sonnet-4-5 --instructions-file reviewer.md
evo-agents hub run dispatch rollout 3 --agent reviewer --project evo-agents
```

The avatar is a PNG, JPEG or WebP image of at most 512 KiB, sent as base64 in JSON (`{"data": ...}`) and checked by
its first bytes; the hub stores it in the blob store as a blob of kind `agent-avatar` the project holds, and serves it
from `GET .../avatar`, whose URL (`icon_url`) carries a part of its SHA-256. Without one, `icon_url` is null and the
web draws the profile's initial on its colour. The audit rows `agent.create`, `agent.update` (with the fields changed)
and `agent.delete` name `<project>/@<handle> v<version>`, never a description, instructions or an image.

Schema 0030 adds the profiles (`evo_agents/hub/tables_profiles.py`): `agent_profiles`, each profile as it is now, and
`agent_profile_versions`, each version of it whole; the default profile of every project, made by the member who made
the project, at version 1; and `profile_id` with `profile_version` on `runs` and on `credential_leases`, both null or
both set, naming a version. Going back to 0029 drops the two tables and the columns: the profiles, and which profile
each run and lease went under, are lost.

## Group chats

A group chat holds people and the agents of projects (`evo_agents/hub/chats.py`). A signed-in member creates one with a
title and the people and agents it starts with, and is its first member. Only its members read it and write to it;
anyone else gets the 404 of a chat that does not exist. Any member adds a person or an agent: adding an agent of project
P needs the writer role on P, and a deleted agent is refused. A person leaves, or the chat's creator takes them out;
any member takes an agent out. A chat holds at most 50 people and 10 agents, and never a person and an agent of the same
name, so that `@name` names one member.

**Label.** A chat's label is the labels of the projects of its agents: for each project, its default label, the one
its runs on no plan read as. Every person in the chat holds a grant on each of those projects whose max level reaches
that project's label, on the project's own ladder: adding an agent that would take the label past someone in the chat
is refused (409, naming who and why), and so is adding someone who does not clear it. The hub checks it again on every
read and write, so someone whose grant was lowered or revoked gets 403. Each message keeps the label the chat had when
it was sent, as a run's events read as its project's: once an agent left and the label went down, a person who joined
since does not read what was written before. A chat is restricted when a project of its agents is (its hub sink cleared
at or above `customer` or `domestic-only`, as Telegram reads it: `docs/notifications.md`).

**Messages and mentions.** A message has at most 16 KiB of UTF-8; `reply_to` puts it in the thread of the message it
answers, and a thread has one level: `thread_root_id` always names its first message. The hub reads a message's
mentions once, when it is sent, and keeps them with it (at most 20 names): `@` then `[a-z0-9-]`, not right after a
letter, digit, `@`, `.`, `/` or `-` (an e-mail address is no mention), not in a code span or a fenced block, ignoring
case. A name resolves among the chat's members only: a person by login (`via` login), an agent by handle (handle), a
project's name to its default agent (project). An agent the sender can see that is not in the chat, or one deleted
since, reaches no one: the mention keeps why, and the hub answers in the message's thread with the reason. A person
mentioned gets the notice `chat_mention`, and one added the notice `chat_added`, never for what they did themselves; in
a restricted chat the notice names the chat and links to it, without sender or text. A message that mentions no agent
makes no run and asks no model anything.

**Agents at work.** An agent mentioned gets the work: a chat run (`evo_agents/hub/chat_runs.py`; `docs/workers.md`,
Chat runs), at most three agents a message, the fourth and on refused in the thread. The run is of the agent's profile
on its project, dispatched by the person who mentioned it, and pinned to a worker of that person that the profile
allows, online, with a free slot (its held runs and the runs queued for it count) and a daemon that runs chat runs,
the one with a checkout of more of the project's repos first; the person must be a writer on the profile's project. No
such worker, and the hub answers in the thread why, worker by worker, with no run. The mention keeps the run it made
(`run_id`) or why it made none. Each (thread, agent, person) is one session: a message to the agent in its thread goes
to the run of the session that is queued or held, in its inbox, and once that run ended done it wakes the session as a
new run that goes on in it (`resume_of_run_id`), on the same worker and branch. Another person's mention starts a
session of their own, on their own worker. In a chat of one person, a message in a thread without a mention goes to the
one agent whose session that person has there (`via` thread); with two people or more, they mention it. The run's
reactions follow it as above, a run that failed says why in the thread, and the agent's reply at the end of each turn
is its message in the thread, with the run that wrote it.

**Budget.** The chat runs of a chat may cost `budget_usd` (5 USD unless its creator changed it, at most 1000) in a
rolling 24 hours, as their usage says (Claude Code's `total_cost_usd`, opencode's `cost`; Codex reports tokens alone
and counts 0, under a time cap instead). A mention once that is spent is refused, saying so in the thread; each run gets
what is left as its cost cap and 30 minutes of agent time. Only the creator changes it (`PATCH /v1/chats/{id}`, audited
as `chat.budget`).

**Agents in chats.** The agent of a run (a chat run, or a step, plan or author run) talks in chats with
`evo-agents worker chat post|react|create|mention` (`docs/workers.md`): a message in its thread or in a chat it is in,
an emoji, a new chat with the person at the origin of the chain (who gets `chat_added`, as does each person it adds,
each with a grant that clears the agent's project), and a mention that hands work to another agent of the chat. That
run is for the same person, at one hop more than the run that asked (a person's mention is hop 1), at most hop 3, never
to the agent's own profile, and within the budget; a refusal is said in the thread. An `@` of an agent in an agent's
reply or post hands no work.

**Reactions.** A reaction is one emoji of one person or agent on a message, once each; a message carries at most 20
different ones, and the hub counts them by emoji (`count`, who put each, and whether the reader did). The reactions of
an agent are set by rule, never by a model: `chat_store.status_reaction` puts the eyes when a worker claims a run of the
agent, a check mark when it is done and a cross when it failed or was cancelled, on the message that started the run,
and the check mark or the cross takes the eyes off. A lost run tried again keeps its eyes. The web shows emoji in
reaction chips only (`web/DESIGN.md`).

**Events.** Each change is an event of the chat, numbered by it: a message, a reaction put or taken off, a member added
or removed. A message is numbered by the event that brought it (`seq`), and `POST .../read` says how far a member read,
which the unread count of the list starts from (their own message reads up to it). `GET .../events?after=SEQ` pages
them, and `GET .../stream` sends them as server-sent events, as a run's stream does: the seq as id, Last-Event-ID or
`after` to go on, a ping after 15 idle seconds, woken by `pg_notify` on channel `evo_chat_events` on the api process's
one LISTEN connection. A stream ends with an `end` event once its reader is no longer in the chat or no longer clears
its label. A member reads the events of the messages whose label they clear. Chat messages do not expire with
`EVO_HUB_RUN_LOG_DAYS`.

| Route | Command | Who |
| --- | --- | --- |
| `GET /v1/chats` | `hub chat list` | anyone: their chats |
| `POST /v1/chats` (`title`, `people`, `agents`) | `hub chat create TITLE [--member LOGIN] [--agent HANDLE]` | anyone |
| `GET /v1/chats/{id}` | | a member |
| `PUT` and `DELETE .../people/{login}` | | a member adds; the person or the creator removes |
| `PUT` and `DELETE .../agents/{handle}` | | a member (adding: writer on its project) |
| `GET .../messages` (`before`, `thread`, `limit`): the chat and a page of messages | `hub chat show CHAT [--thread MESSAGE] [--limit N]` | a member |
| `POST .../messages` (`body`, `reply_to`) | `hub chat send CHAT TEXT [--reply-to MESSAGE]` | a member |
| `POST` and `DELETE .../messages/{message}/reactions` | `hub chat react CHAT MESSAGE EMOJI [--remove]` | a member |
| `POST .../read`, `GET .../events`, `GET .../stream` | | a member |
| `PATCH /v1/chats/{id}` (`budget_usd`) | | its creator |

Creating a chat, adding and removing a member, sending a message and changing the budget are audited (`chat.create`,
`chat.add`, `chat.remove`, `chat.message`, `chat.budget`; an agent's in its project; an agent's message and chat as the
run's dispatcher, naming the run), naming `chat:<id>` and the person, agent or message, never a title or a text. For
example:

```sh
evo-agents hub chat create "Release talk" --member someone-else --agent reviewer
evo-agents hub chat send 3 "@reviewer can you check the diff of run 120?"
evo-agents hub chat react 3 41 👍
```

Schema 0031 adds the chats (`evo_agents/hub/tables_chats.py`): `chats`; `chat_members`, a person or an agent, who
added them (a person, or the run of an agent), when they left, and how far a person read; `chat_messages`, each with
the event that brought it, the first message of its thread, its author (a person, an agent or the hub), the run of the
agent that wrote it, its body and its label; `chat_mentions`, what a message mentions as the hub read it when it was
sent, with the member it reached and the run it made, or why it reached none; `chat_reactions`; and `chat_events`, what
happened in a chat in order. It adds the notices `chat_mention` and `chat_added` too. Going back to 0030 deletes those
notices and drops the six tables: every chat is lost. Schema 0032 adds the chat run: the run kind `chat`, on no plan,
that may go on from an earlier run of its session (`resume_of_run_id`), with `runs.chat_id`, `runs.chat_message_id`
(the message that started it) and `runs.hop` (1 to 3), set for a chat run and null for any other, and
`chats.budget_usd` (more than 0, at most 1000, 5 unless changed). Going back to 0031 deletes the chat runs and drops the
columns.

### What a triage agent may do on its own

The agent of a failed run's triage thread acts for the run's owner through three worker routes,
`POST /v1/worker/runs/{id}/triage/rerun`, `.../triage/plan` and `.../triage/invite` (`evo_agents/hub/server/triage_actions.py`,
`docs/workers.md`, "Failure triage"). The frame is code: `evo_agents/hub/triage_rules.py` allows a rerun for the causes
of its policy table, at most 2 per chain of runs (by `parent_run_id`) and 1 per cause, within the chat's budget, and an
edit of a plan only as a new revision that differs from the held one in the `verify` or `what` of pending steps of the
run's repos, with no command of the `verify` dropped and a `what` only shorter. The whole plan is compared, so a step
that is done, `acceptance`, `goal` and `non_goals` cannot change; the Curator's plans are refused as for a run
dispatched by hand. When the error names another project, `.../triage/invite` puts the agent of that project in the
chat by the rule of Group chats (the owner a writer there, the label of the chat held, else 409 said in the thread),
and the agent hands it work by a mention, within the hop limit of 3. Every success posts a `Decision:` line and an audit row (`run.triage_rerun`,
`run.triage_plan_edit`) in the same transaction.

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
evo-agents hub plan handoff rollout --step 3          # what a new session needs to take step 3 up
evo-agents hub plan complete rollout                  # every step done or descoped: move it to completed
evo-agents hub plan abandon rollout --reason "replaced" [--superseded-by rollout-v2]  # completed as abandoned/superseded
evo-agents hub plan reopen rollout                    # an abandoned plan back to active
evo-agents hub plan export . --commit                 # write the copies, commit exactly the ones that changed
```

`--project` names the hub project when the current directory is not inside the harness. A write names the revision it
read. When someone else wrote in between, the hub answers 409 with the current plan, and `plan step` and `plan patch`
read it again and retry; pass `--if-revision N` to fail instead. `plan put FILE --if-revision N` replaces a whole
plan from a file.

`plan handoff PLAN` prints what a new session or a subagent needs to take the plan up, built by the hub from the plan
it holds (`GET .../plans/{plan_id}/handoff`, the same text as the MCP tool `plan_handoff`): the goal, each step done
with its title, the commit hashes its evidence names, its lines that start with `Decision:` verbatim and the rest of
its evidence cut, then the steps not done with their status and depends_on. `--step KEY` puts the evidence of the
steps that step depends on first. The text stays within `--budget-tokens N` (1000 to 8000, default 4000), counted as
UTF-8 bytes / 3, which is more than a tokenizer counts for English or Vietnamese prose. When it does not all fit, the
goal, the steps it depends on and the steps not done go first, then the other steps done with their Decision lines,
then the rest of the evidence; a part is left out whole, never cut in the middle of a Decision line, and a note at the
end names what was left out and points to `plan_show`. The same revision and budget give the same text, and reading
it needs what reading the plan needs: a grant on the project and a sink that clears the plan's label.

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
Schema 0020 adds the author run (a run of kind `author`, with the member's request in `runs.request`, and a `plan_id`
only when it revises a plan), schema 0021 `plan_revisions.run_id`, the author run that wrote a revision, and schema 0022
the chat of an author run: its wait, park and resume, `runs.finish_requested_at` and the notice `author_waiting`.
Going back to 0019 deletes the author runs and their notices and drops the columns; the revisions they wrote stay,
without the run. Schema 0023 adds `workers.steady_since`, the first heartbeat of a worker's current run of heartbeats,
none more than 30 seconds late, which holds the next attempt of a run lost on a worker back from it until it has been
steady for 120 seconds (`docs/workers.md`, Run states); going back to 0022 drops it. Schema 0024 adds
`runs.failure_cause`, why a run failed as its worker reported it (`docs/workers.md`, Preflight and failure causes);
going back to 0023 drops it, and the Curator's figures read every cause from the error again. Schema 0026 lets a
worker hold up to 16 runs at once: the CHECKs of `workers.slots`, `workers.free_slots` and `worker_pairings.slots` go
from 8 to 16, and the owner changes a worker's slots, labels and name with `PATCH /v1/workers/{id}` (`docs/workers.md`,
Routes); going back to 0025 narrows them to 8 again and refuses, naming them, while a worker not revoked or a pairing
neither used nor expired holds more than 8 slots. Schema 0030 (agent profiles), and 0031 and 0032 (group chats and
the chat run), are described under Agent profiles and Group chats above.

The daemon on the member's machine is the `evo-agents worker` command group, which needs the `worker` extra
(`uv tool install 'evo-ak[worker]'`). It is not `evo-agents hub worker`, the hub's own job worker (see Worker and
queue below). A machine joins with a pairing code from the web's Workers page, or registers directly once signed in
with `evo-agents hub login`.

Runs are driven from any machine signed in to the hub:

```sh
evo-agents hub run dispatch rollout 2 4 --approval auto       # one run per step, all of them queued or none
evo-agents hub run dispatch rollout 5 --worker mac-mini --runtime codex --mode interactive --timeout 90
evo-agents hub run dispatch rollout 6 --runtime claude-code --model opus
evo-agents hub run dispatch rollout 7 --account claude-b     # this account of yours, kept, never failed over
evo-agents hub run plan rollout --worker mac-mini --timeout-h 8  # one plan run: every step not done yet
evo-agents hub run plan rollout --session-per-step            # the same, each step in a new session with the handoff
evo-agents hub run list --state running --state review        # newest first, with the runs in each state
evo-agents hub run show 41
evo-agents hub run logs 41 --follow                           # the live log, until the run ends
evo-agents hub run send 41 "also run ruff before you commit"
evo-agents hub run takeover 41                                # a person drives the agent in a terminal on the worker
evo-agents hub run handback 41                                # the agent goes on headless in the same session
evo-agents hub run approve 41                                 # a run in review: the run and its step are done
evo-agents hub run cancel 41
evo-agents hub run rerun 41                                   # the step again, after a run that ended
evo-agents hub run rerun 41 --account-by-rule                # the same, the rules picking the account
evo-agents hub run candidates 41                              # after it failed: which worker or agent could run it again, and why
evo-agents hub run credentials 41                             # the leases the run got, never their values
evo-agents hub run author "a plan to move the hub's blobs to R2" --worker mac-mini  # an agent writes a new plan
evo-agents hub run author "split step 3 in two" --plan rollout --worker mac-mini   # or revises one
evo-agents hub run chat 42                                    # an author run's chat, and whose turn it is
evo-agents hub run send 42 "keep the old route until 0.10"    # your reply in that chat
evo-agents hub run finish 42                                  # end the chat: the run ends done
```

`run author` queues an author run (`POST /v1/projects/{project}/author-runs`): one run, on one of your workers that
has the project's harness checked out (`--worker`, required), whose agent writes an execution plan from REQUEST
(at most 16 KiB of UTF-8, `-` reads it from stdin) with the create-exec-plan skill the hub holds, and puts it on the
hub as you. `--plan ID` revises that plan instead; `--model` and `--timeout-h` (1, 2 or 4 hours of agent time, 2 by
default) as for `run plan`; `--runtime` is claude-code, the one runtime an author run takes. You need the writer role.
The agent asks and reports in the run's chat: `run chat` prints it, `run send` replies, and `run finish` ends it once
the plan is as you want it (`docs/workers.md`, Author runs). On a worker whose daemon says `author_decisions`, the
agent asks a question with options as a decision instead, which `hub decision list` shows with its run and no plan while
the run has none and `hub decision answer` answers; the run waits for every decision of the turn, or a reply. The web does the same from New plan, Revise with agent and
the run's Chat tab.

Every command after `dispatch`, `plan` and `list` takes the id of a run, as `list` shows it, and finds its project as
the plan commands do: `--project`, or `hub.project` in the harness around the current directory. `dispatch` takes
`--runtime` (`any` by default: the first runtime of the core the claiming worker has; or `<plugin>/<name>`, a runtime
a plugin enabled on the project declares, which the hub refuses with 422 when the project does not register it), `--model` (the model as the runtime names
it, `provider/model` for opencode; the runtime's own choice by default), `--mode` (`headless` by default, or
`interactive`), `--worker` (the id or the name of one of your workers, which pins the runs to it), `--approval`
(`review` by default, which waits for `run approve`; `auto` marks the step done once every verify command the worker
runs again exits 0), `--timeout` in minutes (5 to 240, 60 by default) and `--account` (one of your env secrets
bound to the project, by name as `hub account list` shows it: the runs take that account and keep it, with no
failover; without it the rules of your policy for the project pick, `docs/credentials.md`, section "Accounts").
`run plan`, `run author` and `run rerun` take `--account` too; `rerun` keeps the account picked by hand for the run it
reruns unless `--account` names another one or `--account-by-rule` lets the rules pick.
A model belongs to one runtime, so the
command line takes `--model` only with `--runtime` naming that runtime and refuses it with `any`, before it asks the
hub; the hub itself does not check the pair.

`run plan` queues a plan run (`POST /v1/projects/{project}/plan-runs`): one run, on one of your workers, whose agent
does every step of the plan not done yet, in a worktree of each repo those steps name, and reports each step as it
goes (`docs/workers.md`, Plan runs). It takes `--worker`, `--runtime`, `--model` and `--mode` as `dispatch` does, and
`--timeout-h`, the hours of agent time the run may use: 2, 4, 8 or 24 (4 by default); the time it waits for your
answer to a decision, or parked, does not count. With `--session-per-step` the worker gives each step a new agent
session, whose prompt is the step's with the handoff of the plan (`hub plan handoff`), instead of one session for every
step; only a worker whose daemon says it does that takes the run, and one pinned with `--worker` that does not is
refused (409). The flag is sent only when given, and `show` says which of the two a plan run is; the run's usage
lists each step's session once it ends (`docs/workers.md`, A plan run with a session for each step). The hub refuses
a plan with no pending step, a plan with an active run of any kind, and a step not done that names no repo when the
plan lists more than one. `dispatch`, `rerun` and
`plan` are refused (409, naming the repo, nothing dispatched) when a repo the run needs is one the project lists no
origin for: no credential is leased for it, and the worker's preflight would fail the run anyway. `list` shows each run's
KIND (`step`, `plan`, `review`, `judge` or `author`), and filters by `--state` (repeat it for several), `--plan`,
`--step`, `--worker`, `--by` (the login that dispatched) and `--search`, a page at a time with `--limit` and
`--offset`; `show` of a plan run names its repos, the agent time it used, and when it waited or was parked. `show` of
a queued attempt whose run before it was lost on a worker that is not steady yet has a `waits for` line naming that
worker and when it becomes steady (`steady_wait`; `docs/workers.md`, Run states).

A run that failed has `failure_cause` (in `GET .../runs`, `GET .../runs/{id}`, `run list --json` and `run show --json`,
and a `cause` line of `run show`): the cause its worker reported with the failure, one of `origin`, `credentials` and
`missing_tool` when the preflight stopped it before its agent started, or `push_conflict`, `verify_failed`, `timeout`,
`cost_cap`, `turn_cap`, `time_cap`, `checkout`, `runtime` and `worker_stopped`. It is null for a run that did not
fail, for one an older daemon or the hub itself ended (a lost lease, a revoked worker), and for every run from before
schema 0024; the Curator's figures read the cause of those from the error (`evo_agents/hub/review.py`). A run of one
step or an author run that failed for a cause of the preflight sends its owner the notice `run_failed`, as a plan,
review or judge run does for any failure.

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
Several `env` secrets for one variable are accounts a run picks among; `evo-agents hub account
list|show|set|pause|resume|policy` shows their usage, runs and cost, and weighs, pauses and sets the strategy for them,
as the web's Accounts page does, and `hub account login` adds or renews a Claude account with the token
`claude setup-token` prints (`docs/credentials.md`, section "Accounts").

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

## Local runs

A member's Claude Code session on their own machine shows in the hub as a run of kind `local`, with its machine, cwd,
account (`claude_email`, `claude_profile`), state (`running`, `waiting`, `idle`, `ended`), plan and step and last
activity (`docs/workers.md`, Local runs). It is private to that member in the run list (`hub run list --kind local
--account EMAIL`), the run page, the log and the Monitor. It takes no slot and is never lost, retried or parked, the
Curator ignores it, and one that stays silent for 24 hours ends. Its decisions (category `question`) notify, reach
Telegram and the Inbox like any other, and the first answer wins, from the web, Telegram, `hub decision answer` or the
machine's report that it was answered on the spot. hub-cli-v1 only gained `hub run list --kind --account` and the new
states and kind in `hub run list/show`; the machine side talks HTTP.

## The Curator's review

`docs/curator.md` describes the Curator as a whole: its charter, the night shift and its caps, the tiers, the three
roles, its GitHub App and the ruleset it needs, and Telegram. This section and the next two hold the details.

Each night of a project's charter, the hub reviews the project with a run of kind `review` (schema 0014): the job
`curator.collect` counts the night's figures, without any model, from the session digests, the runs and their tool
figures, the plans and the decisions of the last `review.days` of the charter (7 by default): the failures per tool
and per program, the failures that come from the environment by cause (a command the harness blocked, the hub
answering 5xx, the network, rate limits, credentials, a tool missing), the runs that failed or were lost by cause (the
one the worker reported, else the one read in the error), the
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
  --evidence run:41:7 --evidence code:evo-agents:evo_agents/worker/runner/agent.py:120 --severity high
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
what each state, tier and lens would list with the other filters applied. Accepting a proposal of tier 0 to 2 also
makes it a plan of the night shift (The Curator's changes, below); accepting one of tier 3 records the answer alone.
`proposal show` (`GET .../curator/proposals/{id}`) answers, as `change`, the change accepting it made, when the caller
may read that change's plan: its state, plan, branch, pull request and reason; null otherwise.

The web shows the same under each project's Curator (`/p/{project}/curator`): the night now and the nights before,
the schedule and Pause or Resume, the proposals with their filters, a proposal's evidence (the run's log, the digest's
entry, the line of code on the forge) and draft plan with Accept, Defer and Reject for the project's admins, the
change accepting it made (its state, the plan as a link, the branch on the forge, the pull request and why it waits or
stays open), and the charter with its revisions and, for an admin, its form. Tier 2 proposals in the Inbox open in a sheet of their own
(`/inbox?proposal=ID`), and Home says where each project's Curator stands.

**The morning brief.** At the charter's `brief_at`, in its time zone, the job `curator.brief` sends the owner of the
night shift's schedule a notice `curator_brief` (`evo_agents/hub/server/brief.py`), once a local day: the night it
reports on (the one in progress when `brief_at` falls inside the window), its runs by how they ended and what they cost
against the night's budget, its review run with what it wrote, the merges into a default branch its runs reported, the
project's runs in review that wait for the owner's approval, the Curator's changes still planned with why each waits,
the decisions that wait for the owner's answer and the proposals that wait for an admin's (with how many are in the owner's Inbox), and the worker on duty with its last
heartbeat, so a machine that stopped shows up in the morning. The brief goes out like any notification, on the web and
on every channel the owner turned on, Telegram included (`docs/notifications.md`). A brief the hub could not send
within three hours of `brief_at` waits for the next day. `curator_briefs` keeps each brief with what it said, and `hub
curator status` names the last one (`last_brief`).

## The Curator's changes

Accepting a proposal of tier 0 to 2 makes its draft plan the Curator's plan on the hub, in the same transaction, as
the admin who accepted it (schema 0017, `evo_agents/hub/server/proposal_plans.py`, `evo_agents/hub/judge.py`): the plan
`curator-<proposal>-<slug>`, in the one repo the proposal names, every step pending, on the branch
`curator/<proposal>-<slug>`, and a change of the Curator that says where it stands. A draft that names several repos,
or a repo without an origin, becomes no plan: its change stays `open` with the reason. A write of a Curator's plan that
names another repo or branch, or changes anything but the progress of its steps (`status`, `done_at`, `evidence`,
`note`) and its `status`, is 409: its what, goal, context, verify and acceptance stay as the hub made them. A plan whose
id starts with `curator-` is made this way alone (a member's put of one is 409). No member dispatches the steps of a
Curator's plan or a plan run of it (409): the night shift alone runs it. `evo-agents hub curator proposal plan ID`
(`POST .../curator/proposals/{id}/plan`, the project's admins alone, audited as curator.plan) does the same for an
accepted proposal of tier 0 to 2 that has no change yet, such as one of tier 2 accepted while that tier still recorded
the answer alone; one not accepted, of tier 3 or with a change already is 409 with the reason.

Each night, after its review run, the night shift queues the judge run of a change waiting for one, else the Builder
of a planned change: a plan run of its plan, pinned to the worker on duty, with the night's caps. A change on GitHub
gets a Builder only once the hub checked, within 2 days, that a ruleset keeps the Curator's App off the repo's default
branch; one on GitLab only once the charter names the owner's git secret for it (`git_secret`). The job
`curator.changes` checks that ruleset itself, with the Curator's App and outside any transaction, at its next pass
after the change is planned, and again every hour while the check does not pass and the change waits; it never
creates or changes a ruleset, and a hub without the Curator's App asks GitHub nothing. Each planned change the night
shift passes over keeps why as its reason, written when it changes and cleared when its Builder is queued
(`GET .../curator/changes`, `hub curator changes`, the morning brief). The Builder's GitHub token is the Curator's
App's (`docs/credentials.md`); the Builder and the Judge of a change never run at once.

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
model, the tokens, the session's id and its directory. It also holds the largest context one message of the model read
(`peak_context_tokens`: its input, cache creation and cache read tokens), how many times the context was compacted
(`compactions`, the `compact_boundary` lines of the transcript), the characters of each tool's results
(`tool_result_chars`), and what the spool of the PostToolUse hook did: how many results it cut (`spool_results`, read
from the note it leaves in a result's place), the characters they had before the cut (`spool_original_chars`) and the
calls that named the spool directory, the agent reading a spool file again (`spool_reads`); only these counts leave
the machine, never a spool file. The hub keeps them, and `digest_show` returns them, null in a digest an older client
pushed. The text of `digest_show`, like that of `run_events`, stays under 24,000 characters, below the 25,000 at which
the hub's tools cut a text for `kg_more`: `digest_show` leaves out the last items of its longest lists and says how
many (a kept item keeps the index that `session:ID:FIELD:INDEX` evidence names), and `run_events` stops before the
event that would go past it and says after which seq to call again; one event's line holds at most 500 characters.
Before it leaves the machine every string that looks like a
secret is replaced by `***` (`evo_agents/hub/redact.py`): GitHub, Anthropic, OpenAI, AWS, Slack and Google keys and
tokens, private keys in PEM, hub, web and worker tokens, passwords in URIs, Bearer and Basic credentials, and the value
of any variable or key named as a secret, which is how a lease's value shows in a session; the machine's own hub
token is replaced wherever it appears. The push follows the write rule (the writer role and a hub sink), and the
digest carries the project's default label, so only members whose grant, and the sink they read through, clear it
see it (`GET /v1/projects/{project}/digests`). A later turn replaces the digest; only the member who pushed a session
may. When the hub does not answer, the digest waits in `~/.evo/hub/digest-state.json` and the next Stop that reaches
the hub pushes it, with up to three others that waited. `hub.prune_digests` deletes the digests not pushed for 90
days.

**Long tool output.** PostToolUse (`evo-agents hub hook post-tool-use`, `evo_agents/hub/spool.py`) keeps a long
result out of the model's context. A successful result of Bash, Grep, WebFetch or an MCP tool longer than 10,000
characters is written whole to `~/.evo/tool-output/<session>/<tool_use_id>.txt` (directories 0700, file 0600), and
the model gets, in the tool's own output shape, its first and last 1,500 characters around one line that names the
size and the file. A Read without an offset or a limit is cut only past 40,000 characters, and keeps its first 20,000
characters of whole lines, with no tail, since Claude Code numbers the lines; its line says from which line to Read on
if the rest is needed. The tools of evo-hub and evo-kg pass whole: they cut their own text at 25,000 characters and
give the rest through `kg_more`, and Claude Code saves an MCP text over 50,000 characters to a file itself. So does a
Bash call that prints a plan's handoff (`evo-agents hub plan handoff`), sized by its own budget.

The numbers come from the Claude Code transcripts of one laptop from 2026-10-13 07:41 to 2026-10-18 (669 transcripts,
353 of them subagents, 50,255 tool calls), measured with the scripts of the harness's
`research_notes/Rà soát quản lý context/`. A result counts as saved after the agent's reads of its spool file, and
its reruns of the same call within 10 calls, are taken off:

| Tool | Results | Size | What the numbers decided |
|---|---|---|---|
| Bash | 38,087 | 95th percentile 10,729 | spooled at 10,000, 67% of their characters saved; the band from 10,000 to 15,000 still saved 38%, so the threshold, head and tail stay |
| Read without offset or limit | 914 | median 4,528, 90th percentile 24,307, 95th 32,311 | of 287 cut at 10,000 with a 1,500 head, 91% were read again, 95% of those from the line the note named and 59% to the end, in every size band: 8% saved, 338 extra model calls. No head short of the file avoids that, so more reads stay whole: at 40,000, 36 reads are cut (21% of all Read characters), each keeping 20,000, and the same calls replayed make 74 extra model calls |
| WebFetch | 861 | median 1,382, 95th percentile 23,858 | the 58 over 10,000 held 59% of WebFetch's characters and were never cut, since WebFetch was not in the matcher |
| evo-hub and evo-kg | 440 | 95th percentile 23,514 | cutting them at 10,000 hid what `kg_more` and `plan_handoff` give whole |

`EVO_SPOOL_THRESHOLD` changes the threshold (`0` or `off` turns the hook off), `EVO_SPOOL_READ_THRESHOLD` that of a Read
(`0` or `off` leaves every Read whole; the head is half of it, at most 20,000), and `EVO_SPOOL_DAYS` how long a file is
kept (7 days); the hook deletes older files, at most once an hour. A Bash output over 30,000 characters, which Claude
Code already saves in its session directory with a 2,000-character preview, is copied from that file, so the spool
file holds all of it. A failed call (PostToolUseFailure, whose output Claude Code already cuts to about 10,000
characters of head and tail, enough to see why it failed), an image, a Read with an offset or a limit, and a call that
names `~/.evo/tool-output` pass unchanged. The hook runs in worker runs too, talks to no hub, never sends a spool
file anywhere, and adds the start of `uvx` and the interpreter, about 0.15 seconds, to each Bash, Read, Grep,
WebFetch or MCP call.

Codex does not take this hook. Tried with codex-cli 0.153.4 on 2026-10-12: a command that printed 51,013 characters
reached the model as about 40,000, cut in the middle (`Warning: truncated output (original token count: 13026)` and
`…3026 tokens truncated…`), with no file; a PostToolUse hook gets that cut output, not the whole; Codex ignores
`updatedToolOutput`; and `decision: "block"` does replace the result, but as `Script failed` with the hook's text as
the script's error. The hook therefore leaves a payload from Codex (it carries a `turn_id`) unchanged, and Codex's
own limit, `tool_output_token_limit` in its `config.toml`, is the only knob there.

**Local sessions.** The evo-hub plugin's UserPromptSubmit, PostToolUse (`local-tool`, a group of its own with no
matcher, next to the spool's), Stop and SessionEnd hooks (`evo_agents/hub/local_sync.py`) register a Claude Code
session of a hub project, without `EVO_RUN_ID`, as a run of kind `local` (`PUT /v1/projects/{p}/local-runs/{session}`),
and report its state (`POST .../state`: `running` at a prompt and a tool, `idle` at Stop, `ended` at SessionEnd) and its
log (`POST .../events`, gapless `seq`). The project is the one of the session directory, as for memories; a directory
of none is asked once and remembered in `~/.evo/hub/local/<session>.json`. Sent: session id, directory, machine, the
first line of the first prompt (120 characters), the account (`evo_agents/hub/local_account.py`), the agent's text
(4,000 characters a message), and per tool call its name, input (300 characters) and result (400 characters), all
through `redact_data` on the machine, as the digest is. The account of a Code tab session is read from
`<data folder>/claude-code-sessions/*/*/local_*.json` (the record that holds the session id; its first `*email*` value)
and the claude-profiles profile whose data folder it is; of a CLI session from `oauthAccount.emailAddress` of
`$CLAUDE_CONFIG_DIR/.claude.json` or `~/.claude.json`; otherwise `unknown`. [Unverified] The record's layout was not
checked on a Mac. `EVO_CLAUDE_DESKTOP_DIRS` overrides the data folders.
A plan write (`evo harness step`, `hub plan step`, the MCP `plan_step`, which the proxy sends with the header
`X-Evo-Local-Run`) names the session's run (`CLAUDE_CODE_SESSION_ID` of the tool's environment, else the newest live
session of the directory), so the revision is recorded for the run and the run shows that plan and step; a hub that
does not know the run gets the write again without it. `EVO_HUB_LOCAL_SYNC=0` (or `off`) turns the hooks into no-ops;
the Stop digest and memory push are unchanged. A PostToolUse hook asks the hub at most every 2 seconds, with request
timeouts of 1.5 s and a deadline of 6 s; events wait in the session file (500 at most) while the hub does not answer
(the same cooldown as the digest) or refuses the session (10 minutes), and go in order afterwards. Every hook exits 0.
The questions of the session reach the member too (`docs/notifications.md`, A decision of a local run): a PreToolUse
hook on AskUserQuestion (`local-ask`, 15 s) puts each question of the dialog to the hub and does not wait, PostToolUse
reports the answer given in the dialog, and UserPromptSubmit hands the agent the answers that came from the hub after
it stopped waiting.
[Unverified] The Codex plugin loads the same hooks.json; whether a Codex session registers a local run was not tried.

**Codex.** The evo-hub plugin's SessionStart and Stop hooks run in a Codex session of a hub harness too, so a Codex
session on a laptop gets the same memory sync, plan copies, context line and session digest as a Claude Code one.
Codex installs the plugin from the same marketplace, with
`codex plugin marketplace add https://github.com/maycuatroi1/evo-agents` then `codex plugin add evo-hub@evo-agents`.
Codex calls a hook with the same JSON on stdin as Claude Code (`hook_event_name`, `cwd`, `session_id` and
`transcript_path`, Stop adding `turn_id` and `last_assistant_message`) and injects a SessionStart hook's
`additionalContext` into the session as a developer message before the prompt, so the evo-hub context line reaches
the model. The digest of a Codex session is built (`evo_agents/hub/digest.py`) from the rollout the hook's `transcript_path` names (the
`~/.codex/sessions/.../rollout-*.jsonl` log, whose first line is a `session_meta`), through the same redact on the
machine and with the same fields as a Claude Code digest: `peak_context_tokens` is the largest
`last_token_usage.input_tokens` of the rollout's `token_count` lines (as the Codex runtime measures a run's peak), the
usage comes from the running `total_token_usage` (the cached part moved to `cache_read_input_tokens`), the tools,
commands, edits and errors from the normalized `item_completed` item stream (shell runs, MCP calls, file changes and web
searches), and `compactions` from the `ContextCompaction` items. The spool leaves a Codex result as it is, so
`spool_results` and `spool_original_chars` are 0 and `spool_reads` counts the shell runs that name the spool directory.
The `js`/node_repl and agent-spawning tools that the item stream does not normalize are not counted, and `files_read`
is 0 since Codex reads files through the shell, which the commands already hold.

What Codex requires and does not allow (tried with codex-cli 0.153.4 on 2026-10-12): Codex runs a hook only after its
source is trusted, persisted as a `trusted_hash` under `[hooks.state."evo-hub@evo-agents:hooks/hooks.json:..."]` in
`~/.codex/config.toml`; an untrusted hook is skipped silently in `codex exec`, and `--dangerously-bypass-hook-trust`
runs it without that trust (for automation that vets its own hooks). So the owner trusts the evo-hub hook once on each
machine (the first interactive session that loads it prompts for trust). Codex also clamps a `SessionEnd` hook's timeout
to 3 seconds, but keeps the `SessionStart` (30 s) and `Stop` (25 s) timeouts the evo-hub plugin declares, which is where
its work runs. Codex does not take the PostToolUse spool hook (above). As in Claude Code, the hooks exit 0 whatever
happens and never fail a Codex session.

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
10 and 30 seconds apart; the hub answers what it has already and only the rest is sent. The hub merges the runs of every
machine the way a local corpus does (`evo_agents/kg/corpus.py`). A source whose `ref:`, `repo:` or `path:` changes
starts a new epoch on each machine at that machine's first run after the change, so for the files several machines read,
the runs of the machine that switched last win from then on. `evo-agents hub kg build --project demo --wait` queues a
build and waits for it, and `evo-agents hub kg builds --project demo` lists them. Once a build succeeded, `evo-agents kg
serve --backend auto` and the hub's `/mcp` answer the `kg_*` tools from it, filtered by the read rule.

A build's artifact is the graph's SQLite file in the bucket, and its bytes differ from one build to the next even when
the graph does not. A build whose content hash equals that of the project's latest build still holding an artifact
therefore uploads nothing: it points at that artifact, and `artifact_reused_from` names the build that uploaded it.
Every hour the worker deletes the artifacts of graphs older than each project's `EVO_HUB_KG_KEEP_ARTIFACTS` newest (3 by
default; an artifact several builds share counts once, and the newest always stays). A pruned build keeps its row, its
content hash and its counts, gets `artifact_pruned_at`, and loses `artifact_sha256`, so no row names an object that is
gone. The retention never deletes run logs, source blobs or skill bundles. A hub admin can run it by hand:

```sh
evo-agents hub kg prune --dry-run              # what would go, for every project; changes nothing
evo-agents hub kg prune --project demo --keep 5
```

**Secrets in old copies.** The git connector redacts secrets before a file enters a corpus (see the README), but
runs pushed before it did keep them on the hub, in their source blobs and in the graphs built from them. A hub admin
finds them with `evo-agents hub kg scrub` (every project, or `--project demo`). It reads each built graph and source
blob a project holds in the bucket, page by page (`POST /v1/admin/kg/scrub`, at most 1000 objects and 256 MiB a
page), with the connector's patterns, and lists those holding a secret by hash, kind, size and the kinds of secret,
never a value. A graph is read from the api's cache when it is there. A source blob is read as the file the items of
the project's run logs say it is (their `props.path`), as `evo-agents kg scrub` reads it on a machine: in code a bare
value after `=` is an expression, in a config file or a document a literal. A blob no item names with a path is read
as a document. The first page that meets a source blob that may hold a secret reads every run log of the project for
those paths, so it takes longer; the api keeps them, and reads only the logs of new runs after that. Run logs hold
references only and are not themselves scanned. Each object found has a state. `newest` is the graph the `kg_*`
tools read, and `live` a source blob that a run still in use refers to: a run waiting for its commit, the newest full
run of its source (a complete listing and an ok end), or a run after that. Anything else is `old`. Counting changes
nothing.

`--purge` says what it is about to delete and asks first (`--yes` when no terminal can answer), then has the hub
delete the old objects (`POST /v1/admin/kg/scrub/purge`, at most 1000 hashes a request). The hub reads each one again
and keeps those that hold no secret any more, are in use, or are not the project's, saying why. For the rest, in one
transaction holding the blob lock, it drops the project's `blobs` rows, marks the builds of a deleted graph pruned as
the retention does, records the objects no other project holds for deletion, and writes one `kg.scrub` audit row
counting what went. Then it deletes the objects and drops the deleted graphs from its cache. Delete in this order, so
the newest graph and the live blobs are the redacted ones and nothing the project uses holds a secret:

```sh
evo-agents kg sync --build --push              # on each machine: runs with the secrets redacted
evo-agents hub kg build --project demo --wait  # the newest graph, from the redacted runs
evo-agents hub kg scrub                        # count, change nothing
evo-agents hub kg scrub --purge                # delete the old copies, after a yes
```

Which blob is live is a guess from the newest full run of each source. When the merge kept an older record (a removal
the deletion guard held back, or an older checkout pushed after a newer one), a blob in use may count as old. Deleting
it makes the project's next build fail, naming the blob, and pushing a new run of that source mends it. The worker's
cache under `<data dir>/kg/home` holds the corpus as a machine's kg home does, old blobs included:
`EVO_KG_HOME=<data dir>/kg/home evo-agents kg scrub --all --purge --yes` in the worker's container cleans it the same
way, or delete the cache, and the next build fetches what it needs again.

## Plugins

A plugin is a versioned bundle, a tar.gz with its manifest `evo-plugin.json` at the top, checked against
`evo_agents/harness/schemas/plugin.schema.json` (seam plugin-v1): the plugin's id, version, publisher, tier, the
evo-agents versions it runs on (`engines`), the plugin API it is written for (`plugin_api`), the JSON Schema of its
config, the permissions it asks for and what it contributes to each extension point. `evo_agents.plugins` reads,
packs, checks and places a plugin for the hub and the worker alike. An isolated plugin, the default tier, is installed
on a hub from its store; a core plugin runs Python in the hub's and the worker's processes and is a Python package
installed into the image, which declares itself in the entry point group `evo_agents.plugins`. The registries of
runtime adapters and of kg connectors read the core plugins' contributions beside their own groups
`evo_agents.worker.adapters` and `evo_agents.kg.connectors`, which keep working and win over them. What a plugin adds
to the hub (channels of notifications, routes under `/v1/x/<plugin>/`, MCP tools named `<plugin>__<tool>` on `/mcp`,
periodic jobs, events delivered at least once, pages in the project's part of the web, and the tables of a core plugin)
works only in a project that enabled it, and each point has a version of its own: `docs/plugins.md`, sections Extension
points and Pages.

A publisher signs each bundle, and the operator lists the publishers the hub trusts in `EVO_HUB_PLUGIN_PUBLISHERS`
(section Deploying). A publisher signs keyless with Sigstore from the CI workflow of its plugin repository
(`sigstore sign` in GitHub Actions writes `BUNDLE.sigstore.json`); the hub accepts the signature when its
certificate's issuer is GitHub Actions and its identity a run of the workflow the operator named, the repository's
name compared ignoring case and the workflow's path exactly. A hub that cannot reach Sigstore trusts Ed25519 keys of
the publisher instead (`evo_agents.plugins.signing.sign_ed25519` makes the signature). Only a publisher listed in
`EVO_HUB_PLUGIN_CORE_PUBLISHERS` may publish a core plugin.

The store keeps every version a hub admin published, never changed: the bundle in the blob store, held by the hub
itself (kind `plugin-bundle`, outside any project), its SHA-256, size, manifest, publisher, signature and who signed it
(tables `plugins` and `plugin_versions`, migration 0034). A plugin belongs to the publisher of its first version. Only
a hub admin uses the store, through `/v1/plugins`, the web (Administration, Plugins, which also lists the publishers
the operator trusts, from `GET /v1/plugin-publishers`) or the command line:

```sh
evo-agents hub plugin publish sandbox-1.2.0.tar.gz   # with sandbox-1.2.0.tar.gz.sigstore.json or .sig.json beside it
evo-agents hub plugin publish sandbox-1.2.0.tar.gz --signature release.sig.json
evo-agents hub plugin list                           # state, installed and latest version, the upgrade waiting
evo-agents hub plugin show sandbox                   # every version, who signed it and what it asks for
evo-agents hub plugin install sandbox                # the newest version that runs on this hub, or --version 1.2.0
evo-agents hub plugin upgrade sandbox                # the newest above the installed one, or --version
evo-agents hub plugin remove sandbox                 # every version stays in the store
```

Publishing reads the committed bundle back from the blob store and checks it whole before it records anything, and
installing or upgrading checks the stored bytes again against the publishers and the release the hub has then. A
refusal is a 422 whose message names its reason in parentheses and what to do: `malformed` (not a bundle, a manifest
that does not read, a signature that is not one), `unsigned`, `altered` (the bytes are not the ones signed, or not the
ones the store recorded), `untrusted` (a publisher the operator does not list, a workflow or key it did not name, a
core plugin of a publisher that is not core), `invalid` (the signature does not verify) and `incompatible` (outside
the plugin's `engines` or `plugin_api`). A signature the hub cannot check now, Sigstore's trust root out of reach, is a
503 and records nothing. Each write is audited as `plugin.publish`, `plugin.install`, `plugin.upgrade` or
`plugin.remove`, naming `plugin:<name>@<version>`.

As the api starts, it turns off every installed plugin whose version no longer runs on its release, the hub having
been upgraded past the plugin's `engines`: the plugin shows `disabled` with the reason, the audit trail
`plugin.disable`, and every hub admin who has signed in gets the notice `plugin_disabled`. The hub serves on, and a
failure of that check is logged without stopping the start. Upgrading the plugin to a version that runs, or installing
one again, turns it back on. A core plugin of the hub's image is not checked so: the image loads it only on a release
it runs on, and records it in the store as the api starts (`docs/plugins.md`, section Core plugins).

### Isolated backends and the plugin host

An isolated plugin carries a backend: a program the hub runs outside the api and the worker, over the `plugin/1`
protocol, in the **plugin host**, `evo-agents hub plugin-host`. The host is a service of its own, built from the same
image (the `plugins` service of the compose file), that holds none of the hub's variables: not `EVO_HUB_DSN`, not
`EVO_HUB_SECRETS_KEY`, not the blob store keys. It is given the hub's URL, the shared secret
`EVO_HUB_PLUGIN_HOST_TOKEN` and a data directory, pulls the installed isolated plugins from the api's routes under
`/internal/plugin-host/` (outside `/v1`, so the reverse proxy never forwards them), and runs each backend in a process
of its own built from nothing, under a user of its own: the service runs as root with only SETUID, SETGID, CHOWN and
KILL, its volume at `/plugins`, and a host that is not root starts no backend. The api reaches the host at
`EVO_HUB_PLUGIN_HOST_URL` with the same shared secret, which must be at least 32 random characters and never a value
an example showed; without both variables the api runs no backend and the service idles. The hub mints a backend's
token at each start and revokes a plugin's tokens when it is removed or turned off. A backend that crashes or hangs
makes the call fail within its timeout, and the host restarts it with a backoff; a hub admin reads each backend's
state at `GET /v1/plugins/{name}/backend`. The compose file caps the service with `EVO_HUB_PLUGINS_CPUS`,
`EVO_HUB_PLUGINS_MEMORY` and `EVO_HUB_PLUGINS_PIDS`. The protocol, the sandbox, the requirements a backend may name
(hashed wheels in its bundle) and how an author checks a plugin with `evo-agents plugin test` are in
`docs/plugins.md`.

### Plugins in a project

An installed plugin does nothing in a project until an admin of the project enables it there (table `project_plugins`,
migration 0035). A core plugin of the hub's image with nothing per project, Telegram, runs hub-wide instead: the store
lists it, and a project's plugins do not (`docs/plugins.md`, section Core plugins).
`GET /v1/projects/{project}/plugins/{name}` shows what the installed version asks for, each permission with what it
lets the plugin do in the project; enabling grants exactly that list, which the request names
back, and any other list is a 422. Once an upgrade asks for more, the plugin lists it under `pending`, and enabling it
again grants the new list. What a plugin may do in a project is what was granted there that its installed version
still asks for, and only while it is installed and turned on; `granted` says that grant, so an upgrade that asks for
less narrows it at once. Disabling deletes the grant; the versions of its config stay. A call the web's bridge makes
for a plugin's page carries `X-Evo-Plugin: <plugin>`, and the hub refuses it with 403 unless the plugin may make it
now (docs/plugins.md, Pages). Readers and writers of the project read all of this; only its admins change it. The web does the same on the
project's Plugins page: Enable opens the consent, which lists every permission in words and the pages the plugin adds,
and grants exactly that list; a plugin's page there holds the form of the project's layer of its config.

```sh
evo-agents hub plugin list --project evo-agents                  # each installed plugin, enabled or not, what it asks for
evo-agents hub plugin enable sandbox --project evo-agents        # prints what it asks for, then asks; --yes grants it
evo-agents hub plugin disable sandbox --project evo-agents
```

A plugin enabled in a project also registers its runtimes there, `<plugin>/<name>` (`docs/plugins.md`, Runtimes):
the runs of the project may ask for them, and a dispatch of a runtime the project does not register is a 422. Schema
0039 lets `runs.runtime` and `runs.requested_runtime` hold such a name and adds `workers.plugins`, the plugins whose
runtimes a worker takes; going back to 0038 deletes the runs of plugins' runtimes and drops the column.

It registers its environments there too, `<plugin>/<name>` (`docs/plugins.md`, Environments), which the environment
axis of the project's agent profiles names. Schema 0040 adds the axis to every profile, with mode `all`, and
`runs.environment`, the environment of a plugin a run's claim picked (NULL for the host); going back to 0039 drops the
column and takes the axis out of every profile, whose runs then run on the host.

Its agent types are listed there too (`docs/plugins.md`, Agent types): an admin of the project enables one with `PUT
/v1/projects/{project}/agent-types/{plugin}/{name}`, which creates its agent profile the first time and names the
member its runs go under (`run_as`), the workers of that member they may take and the cap of their cost in 24 hours.
`DELETE` on the same path turns it off. The plugin, granted `runs:dispatch`, hands it tasks with its token
(`/v1/plugin-api/`): each a run of kind `task` on the branch `task/<run>` of the project's repos. The hub refuses a
task once the type's runs cost its cap, opens a draft pull request of each pushed branch and never merges it, and tells
the plugin the end with the event `task.result`. Schema 0042 adds the table `project_agent_types`, the kind `task` of
runs with `runs.agent_type_id` and `runs.pulls`, and the delivery kind `task.result`. Going back to 0041 deletes the
task runs and their deliveries, and drops the columns and the table.

The core plugin builder (`docs/plugins.md`, Builder) keeps what its agent proposes until an admin decides it. Schema
0043 adds the table `builder_proposals` (each proposal's kind, target, change, summary, the setting's version it was
proposed at, the run, chat and thread it came from, and once decided its state, `applied_version`, `error` or
`rollback_version`) and the notice kind `builder_proposal`. Going back to 0042 deletes those notices and drops the
table.

A plugin's config is checked against the JSON Schema of its installed version and comes in layers, lowest first: the
schema's defaults, the hub's own layer (instance, written by hub admins), the project's, an agent profile's of the
project (agent) and a run's own (`plugin_config` of `POST .../runs`, plain values only). A field takes its value from the
highest layer that sets it, whole. The three stored layers keep every version (table `plugin_configs`): a write names
the fields it sets and those it unsets, the others stay, and each write that changes something makes the next
version; a rollback makes the next version a copy of an older one. A field the schema marks `"x-secret": true` is
sealed with `EVO_HUB_SECRETS_KEY` (without the key, setting one is a 503) and never shown again: answers name it as
set, and the digest of an effective config counts it by a fingerprint of its sealed value. When a worker claims a
run, the hub records, for each plugin enabled on the run's project, the installed version and the digest of the run's
effective config (table `run_plugins`, `GET .../runs/{id}/plugins`).

```sh
evo-agents hub plugin config sandbox --project evo-agents --set greeting=hi --set retries=2
printf %s "$TOKEN" | evo-agents hub plugin config sandbox --project evo-agents --secret token
evo-agents hub plugin config sandbox --project evo-agents --agent helper --set retries=4
evo-agents hub plugin config sandbox --instance --set greeting=hello      # the hub's own layer, hub admins
evo-agents hub plugin config sandbox --project evo-agents --versions
evo-agents hub plugin rollback sandbox --project evo-agents --to 1
evo-agents hub plugin config --project evo-agents --effective             # every plugin it enabled, for the default agent
evo-agents hub plugin config --project evo-agents --run 412               # what run 412 ran with
```

Each change is audited in the project: `plugin.project-enable` and `plugin.grant` (with the permissions) when a plugin
is enabled, `plugin.grant` when a grant changes, `plugin.project-disable`, and `plugin.config` for each new version of a
layer, naming the fields that changed or the version a rollback copied, never a value.

## Branding and features

A hub admin gives the hub a brand of its own and turns off what its users do not need, from the web (Administration,
Branding and Features) or the API. Neither needs a variable or a new image, so one image serves hubs that look and
behave differently (`docs/hub-deployment.md`). A hub where nothing is set looks and answers as one without either.

**Branding** is the product's name (60 characters at most), a logo that takes the mark's place, a favicon, and the
colours of four tokens of `web/DESIGN.md` for each theme: `brand`, `brand-hover`, `brand-soft` and `on-brand`, and no
other colour. A write is refused (422, naming the pair, its colours and its ratio) when the colours it leaves, each
theme whole and an unset token at its default, miss a contrast of WCAG 2.2 AA: `brand` on `surface`, `canvas` and
`brand-soft` at 4.5:1 and on `surface-selected` at 3:1, `brand-hover` on `surface` at 4.5:1, and `on-brand` on
`brand` at 4.5:1. The logo and the favicon are PNG, JPEG or WebP images (the favicon may also be an ICO) of 256 KiB at
most, sent as base64 in JSON; the hub keeps them in the blob store as blobs it holds itself (kind `branding-image`), so
setting one needs the S3 variables. Unsetting a field brings back the web's own name, mark, favicon or colour.
`GET /v1/branding` answers without a credential, since the sign-in page shows it.

**Features** are what core declares can be turned off (`evo_agents.hub.features`):

| Feature | Set for | Off means |
| --- | --- | --- |
| `chats` | the hub | no Chats in the menu; `/v1/chats` and the paths below it answer 404 |
| `curator` | the hub or a project | no Curator in the project's menu; `/v1/projects/{project}/curator...` answers 404 and the `/mcp` tool `curator_figures` returns that error; the night shift, its figures and the morning brief skip the project |
| `kg` | the hub or a project | no knowledge graph in the project's menu; `/v1/kg/{project}...` answers 404 and the `kg_*` tools of `/mcp` return that error |
| `monitor` | the hub | no Monitor page; it owns no route, it reads the overview Home reads |

A feature is on until a layer turns it off. A project's layer, where it sets a feature, wins over the hub's, so a hub
admin may turn a feature off for the whole hub and back on for one project. The 404 comes after authentication, so a
request without a credential still gets 401, and a caller who cannot see the project gets the 404 of a project that
does not exist. The web leaves out of its menus what is off for the project it shows, and answers 404 to its pages.

Both are settings with versions, kept in `hub_settings` (migration 0037): a write names the fields it sets (`values`)
and those it clears (`unset`), the others stay, and `if_version` makes it a 409 unless the layer is at that version
now; a rollback makes the next version a copy of an older one. Each version is audited as `settings.branding` or
`settings.features`. There is no command for them; a hub admin's machine token works with the API:

```sh
H="authorization: bearer $(cat ~/.evo/hub/token)"   # a hub admin's machine token, from evo-agents hub login
curl -fsS -H "$H" -H 'content-type: application/json' https://hub.example.org/v1/admin/branding \
  -d '{"values": {"name": "Acme Agents", "colors": {"light": {"brand": "#0b5cad"}, "dark": {"brand": "#6fb1ff"}}}}'
base64 < logo.png | jq -Rs '{data: gsub("\n"; "")}' | curl -fsS -X PUT -H "$H" -H 'content-type: application/json' \
  https://hub.example.org/v1/admin/branding/logo -d @-
curl -fsS -H "$H" -H 'content-type: application/json' https://hub.example.org/v1/admin/features \
  -d '{"values": {"chats": false, "kg": false}}'
curl -fsS -H "$H" -H 'content-type: application/json' https://hub.example.org/v1/admin/projects/demo/features \
  -d '{"values": {"kg": true}}'                                          # kg back on for demo alone
curl -fsS -H "$H" -H 'content-type: application/json' https://hub.example.org/v1/admin/branding/rollback \
  -d '{"version": 1}'
```

## Blobs on R2

Postgres holds metadata; the blob store holds bytes. The bucket stays private, and the access key the hub gets should
be limited to that one bucket. Keys:

- `blobs/sha256/<sha256>`: a blob, written only by the hub and only with bytes whose SHA-256 the hub computed. A
  blob never changes. The hub deletes only built graphs that the retention dropped, and the old graphs and source
  blobs holding a secret that an admin's scrub deleted (see above), and only once no row refers to them. The `blobs`
  table says which projects hold each.
- `uploads/<upload_id>` and `uploads/<upload_id>.sealed`: an upload in progress, and the hub's own copy of it.

An upload takes three calls. The client asks `POST /v1/blobs/uploads` for presigned PUT URLs (valid 15 minutes, for
the declared size only), PUTs the bytes straight to R2, then calls `POST /v1/blobs/commit`. The hub reads each upload
back, hashes it, and copies it under `blobs/sha256/` only when size and hash match what the client declared. A blob
another project holds must still be uploaded and checked, so knowing a hash gives nobody another project's bytes.
Limits per kind: 10 MiB for a skill bundle, 32 MiB for a plugin bundle, 256 MiB for a run log, 64 MiB for a source
file.

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
- `hub.prune_audit`, daily at 04:33: deletes the audit rows older than `EVO_HUB_AUDIT_DAYS` (365 by default) and logs
  how many (`audit pruned`, with `deleted` and `days`).
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
  review run, merges, runs waiting for approval, the Curator's changes still planned with why, open decisions and
  proposals, and the last heartbeat of the worker on duty (`evo_agents.hub.server.brief`).
- `curator.changes`, every minute: the Curator's changes move on: their pull requests opened and read for signs of
  score hacking, the Judge's check runs written, the tier 0 ones merged when everything allows it and the others left
  open for their owner, read again hourly for a merge or a close by hand; and the rulesets checked: the one a planned
  change waits for at the next pass and hourly while it does not pass, every other once a day (The Curator's changes,
  above).
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
- `CREATE`, `ALTER` and `DROP` of a test's database and role in `tests/hub/pg.py`, and the `REASSIGN OWNED` that gives
  a copy of the migrated template to its own role, run by the superuser, where SQLAlchemy has no construct either.

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

- `ghcr.io/maycuatroi1/evo-agents-hub:<version>`, from `deploy/hub/Dockerfile`: api, worker and the plugin host. It
  runs as a non-root user (the plugin host alone runs as root, section Isolated backends), serves on 8080, and its
  HEALTHCHECK calls `/v1/health/live`.
- `ghcr.io/maycuatroi1/evo-agents-hub-web:<version>`, from `web/Dockerfile`: the Next.js standalone server on 3000.
  `next build` fixes the `/v1` rewrite into the image, and the build argument `EVO_HUB_API_INTERNAL_URL` sets its
  target, `http://api:8080` by default. Build with `--build-arg EVO_HUB_API_INTERNAL_URL=<url>` for another address.

`deploy/hub/docker-compose.yml` builds the four services (api, worker, the plugin host `plugins`, and web) from the
checkout it is in and gives the images those names, tagged `EVO_HUB_VERSION`; `docker compose up` without `--build`
pulls that release from GHCR instead. Postgres is not in it: point `EVO_HUB_DSN` at a database the platform manages,
reachable on the Docker network `EVO_HUB_PLATFORM_NETWORK` (default `dokploy-network`). The volumes `api-cache` and
`worker-cache` are caches; deleting them loses nothing. `plugins-cache` holds the code and the environments of the
isolated plugins, which the host places again, and each backend's own data directory, where a plugin may keep state:
deleting it loses that state. A hub for a client, from the same images with its own branding, publishers and plugins,
is in `docs/hub-deployment.md`. Set the variables in the platform's environment, never in a committed file.
`deploy/hub/.env.example` lists them:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `EVO_HUB_VERSION` | compose | image tag: a release's tag without the `v` to pull it, or a name such as `main` for images built from the checkout |
| `EVO_HUB_DSN` | api, worker | `postgresql://` URI of the hub database (required) |
| `EVO_HUB_ADMINS` | api | GitHub logins of hub admins, comma-separated |
| `EVO_HUB_GITHUB_CLIENT_ID` | api | the OAuth App's client id; without it nobody can sign in |
| `EVO_HUB_GITHUB_CLIENT_SECRET` | api | the OAuth App's secret, for web sign-in and the App check |
| `EVO_HUB_SESSION_SECRET` | api | at least 32 random characters; signs session and CSRF values and keys the hashes of worker pairing codes, which answer 503 without it |
| `EVO_HUB_PUBLIC_URL` | api | the URL browsers use, such as `https://hub.example.org` |
| `EVO_HUB_S3_ENDPOINT`, `EVO_HUB_S3_BUCKET`, `EVO_HUB_S3_ACCESS_KEY_ID`, `EVO_HUB_S3_SECRET_ACCESS_KEY` | api, worker | the blob store, all four or none; for R2 the endpoint is `https://<account id>.r2.cloudflarestorage.com` |
| `EVO_HUB_S3_INTERNAL_ENDPOINT` | api, worker | optional: where the hub itself calls the same store, such as `http://seaweedfs:8333` on the platform's network when `EVO_HUB_S3_ENDPOINT` is a public hostname behind a proxy that caps a request's body (Cloudflare takes 100 MB); presigned URLs keep `EVO_HUB_S3_ENDPOINT`; set without the four, the hub does not start |
| `EVO_HUB_SENTRY_DSN` | api, worker | optional error reporting |
| `EVO_HUB_LOG_LEVEL` | api, worker | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |
| `EVO_HUB_BLOB_CONCURRENCY` | api | uploads one process checks and copies in the blob store at once, every commit together; default `32`, at most `256` |
| `EVO_HUB_KG_KEEP_ARTIFACTS` | api, worker | newest built graphs of each project whose artifact stays in the bucket; older ones are deleted every hour; default `3`, at least `1` |
| `EVO_HUB_RUN_LOG_DAYS` | worker | days the events of a finished run are kept before the daily pruning deletes them; default `30`, from `1` to `3650` |
| `EVO_HUB_AUDIT_DAYS` | worker | days an audit row is kept before the daily `hub.prune_audit` deletes it; default `365`, from `30` to `3650` |
| `EVO_HUB_POOL_MIN_SIZE`, `EVO_HUB_POOL_MAX_SIZE`, `EVO_HUB_POOL_TIMEOUT` | api, worker | the connection pool: its smallest and largest size and the seconds a request waits for a connection; defaults `1`, `10` and `10` |
| `EVO_HUB_RUN_LEASE_SECONDS` | api | how long a claim and each heartbeat of a worker daemon lease a run for before the reaper finds it lost; default `300`, from `5` to `3600`, and well above the daemon's heartbeat of 15 seconds (the end-to-end tests shorten it) |
| `EVO_HUB_DECISION_WAIT_SECONDS` | worker | how long a plan run waits for its owner's answer before the reaper parks it; default `86400` (a day), from `1` to `604800` (the end-to-end tests shorten it) |
| `EVO_HUB_SECRETS_KEY` | api, worker | 32 random bytes in base64url that seal the credentials of worker runs (`docs/credentials.md`); without it writing a secret answers 503 and runs get no lease. It is not in the database or its dumps |
| `EVO_HUB_GITHUB_APP_ID`, `EVO_HUB_GITHUB_APP_PRIVATE_KEY` | api, worker | the GitHub App that makes each run a token for its repos only: its ID or client ID, and its private key in PEM, where `\n` may stand for each line break; both or neither |
| `EVO_HUB_CURATOR_APP_ID`, `EVO_HUB_CURATOR_APP_PRIVATE_KEY` | api, worker | the Curator's own GitHub App, evo-agents-curator, as the two above: every run of the Curator gets its GitHub token from it alone, and the hub checks with it that each repo's ruleset keeps it off the default branch; both or neither, and without them no run of the Curator gets a GitHub token (`docs/curator.md`) |
| `EVO_HUB_TELEGRAM_BOT_TOKEN`, `EVO_HUB_TELEGRAM_WEBHOOK_SECRET` | api, worker | the hub's Telegram bot, as @BotFather gives its token, and the secret Telegram sends back with each update (1 to 256 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`); without either the Telegram channel is off and the hub runs on (`docs/notifications.md`). `evo-agents hub admin telegram --set-webhook` then points the bot at `EVO_HUB_PUBLIC_URL/v1/telegram/webhook` |
| `EVO_HUB_PLUGIN_PUBLISHERS` | api | the publishers whose signed plugin bundles the hub accepts: comma-separated `ID=sigstore:OWNER/REPO/.github/workflows/FILE.yml` (a GitHub Actions workflow that signs keyless with Sigstore) or `ID=ed25519:KEY` (a public key, 32 bytes in base64) entries, several for one id; unset trusts nobody and every bundle is refused (section Plugins) |
| `EVO_HUB_PLUGIN_CORE_PUBLISHERS` | api | the ids among those publishers whose plugins may be core plugins, comma-separated; each must be in `EVO_HUB_PLUGIN_PUBLISHERS` |
| `EVO_HUB_PLUGIN_HOST_URL` | api, worker | where the api reaches the plugin host, such as `http://evo-agents-hub-plugins:8090`; with the token below, both or the api runs no isolated backend (section Isolated backends) |
| `EVO_HUB_PLUGIN_HOST_TOKEN` | api, worker, plugins | the secret the api and the plugin host share, at least 32 random characters, refused when it is a value an example showed; the `plugins` service takes it and nothing else of the hub's |
| `EVO_HUB_FORWARDED_ALLOW_IPS` | api | the reverse proxies whose `X-Forwarded-For` the api believes: IP addresses or networks, comma-separated, or `*`; unset keeps uvicorn's default, the loopback addresses (or its own `FORWARDED_ALLOW_IPS`) |
| `EVO_HUB_WORKER_CPUS`, `EVO_HUB_WORKER_MEMORY` | compose | worker limits, default `2` and `4g` |
| `EVO_HUB_PLUGINS_CPUS`, `EVO_HUB_PLUGINS_MEMORY`, `EVO_HUB_PLUGINS_PIDS` | compose | limits of the `plugins` service, every backend together, default `1`, `2g` and `2048` |
| `EVO_HUB_API_INTERNAL_URL` | web | where the web server reaches the api, default `http://evo-agents-hub-api:8080` (the api's network alias) |
| `EVO_HUB_WEB_TIME_ZONE` | web | time zone of dates rendered on the server, default `Asia/Ho_Chi_Minh` |
| `EVO_HUB_PLATFORM_NETWORK` | compose | the external network the database is on |

The compose file passes api and worker every variable `evo_agents/hub/config.py` reads, an empty value being unset,
so the platform's environment reaches each one. It leaves out `EVO_HUB_HOST`, `EVO_HUB_PORT` and `EVO_HUB_DATA_DIR`,
which the image sets, and `EVO_HUB_GITHUB_URL`, `EVO_HUB_GITHUB_API_URL`, `EVO_HUB_GITHUB_TIMEOUT` and
`EVO_HUB_TELEGRAM_API_URL`, which only tests change. `tests/hub/test_compose.py` fails when the configuration reads a
variable that the compose file or `deploy/hub/.env.example` lacks, and prints the lines to add; a variable left out on
purpose goes in its `NOT_PASSED` with the reason. A missing or malformed variable stops the process with a log line
naming it.

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

### Deploying main

`.github/workflows/deploy.yml` deploys every push to main once `ci` passes on it. The platform (Dokploy) holds the
compose file from the git branch `main` with `EVO_HUB_VERSION=main`, and builds the three images there with
`docker compose up -d --build`; no image of main goes through GHCR. The job:

1. stops when main has moved past its commit, since the platform clones main as it is and the newer commit's own `ci`
   run deploys it;
2. reads the schema the hub runs from `/v1/health`, fails when this commit does not know it, and runs the platform's
   Postgres backup first when the newest file under `evo_agents/hub/migrations/versions/` is another revision, or
   when the hub does not answer, since api and worker migrate as they start;
3. asks the platform to deploy, waits for the deployment to end `done`, then for `/v1/health` to report `status` ok
   and this commit's schema.

The environment `production` of the repository, open to main only, holds the secret `DOKPLOY_API_KEY` and the
variables `DOKPLOY_URL`, `DOKPLOY_COMPOSE_ID`, `DOKPLOY_BACKUP_ID` and `EVO_HUB_URL`. A run of the workflow by hand
(`workflow_dispatch`) deploys main again. `/v1/health` reports the version of the last release on main, not the
commit: the Dokploy deployment names it, with the commit's message as title and `Commit: <sha>` as description.

### Running the stack locally

`deploy/hub/docker-compose.dev.yml` adds, on top of the production file, which builds the images from this checkout,
Postgres 16, moto as the S3 store, and a one-off service that creates the bucket. `deploy/hub/.env.example` works with it as
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
