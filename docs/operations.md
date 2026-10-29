# Operating a hub and its workers

Start with [Getting started](getting-started.md) to connect a machine and run your first plan.
This guide collects day-to-day commands and links to the full reference.

## Workers

A worker is your own laptop or desktop running plan steps for you. You dispatch a ready step, the hub queues a run,
and your worker claims it, runs the step with Claude Code, opencode or Codex CLI, and sends its log, state and
evidence back, so the hub can record the step in the plan. The daemon needs the `worker` extra:

```sh
uv tool install 'evo-ak[worker]'    # or: pip install 'evo-ak[worker]'; with graphify: 'evo-ak[graphify,worker]'
evo-agents worker join --url https://hub.example.org --code K7QM-4XPD   # the code from the hub's Workers page
evo-agents worker register --name mac-mini --project demo --slots 1    # or this, once signed in with hub login
evo-agents worker service install   # keep the daemon running in the background
evo-agents worker doctor            # what the machine still holds that a worker should not; exit 2 on a high finding
```

A daemon that `worker service install` runs from a `uv tool install` of evo-ak, from git or from PyPI, updates itself
to the tag `nightly` once it and every daemon of its install are idle, and starts again on the new version; lock its
version on the worker's page to keep it where it is. A team that follows nightly installs from it:
`uv tool install --force 'evo-ak[worker] @ git+https://github.com/maycuatroi1/evo-agents@nightly'`.
[docs/workers.md](workers.md#updating-itself-to-nightly) has the conditions, the states and how to turn it off.

Then, from any machine signed in to the hub:

```sh
evo-agents hub run dispatch rollout 2 --project demo
evo-agents hub run logs 41 --follow --project demo
evo-agents hub run approve 41 --project demo
```

A worker can also take a whole plan as one plan run: its agent does every step not done yet in the order
`depends_on` allows, in a worktree of each repo those steps name, and reports each step as it goes, verified, committed
and pushed on the plan's branch. Decisions that require a person under the run's [autonomy policy](autonomy.md) wait in the Inbox on the web or on the command line:

```sh
evo-agents hub run plan rollout --worker mac-mini --timeout-h 8 --project demo
evo-agents hub run plan rollout --session-per-step --project demo   # each step in a new session, with the handoff
evo-agents hub decision list --state open --project demo
evo-agents hub decision answer 7 --option postgres --project demo
evo-agents hub notifications --unread
```

A plan run does every step in one agent session unless it is dispatched with `--session-per-step`: then each step
gets a new session whose prompt carries the plan's handoff instead of what the sessions before it read, and the run's
usage lists the session of each step. A Claude Code agent on a worker compacts its context as it nears 200,000 tokens
(`EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW` on the worker sets another window). A Codex agent compacts there too and
keeps 8,000 tokens of each tool output (`EVO_WORKER_CODEX_AUTO_COMPACT_TOKEN_LIMIT`,
`EVO_WORKER_CODEX_TOOL_OUTPUT_TOKEN_LIMIT`), and opencode clears old tool outputs (`EVO_WORKER_OPENCODE_COMPACTION_PRUNE`
turns that off); what the owner's own codex or opencode config sets stays.

The agent runs with the operating-system access of the worker process. Workers may be private or
[shared](workers.md#shared-workers); the hub checks who may dispatch and the run's autonomy policy.
[docs/workers.md](workers.md) describes the protocol and plan runs, [docs/hub.md](hub.md) every `hub run`
and `hub decision` command, and [docs/notifications.md](notifications.md) decisions, notices and notifications.

On a machine others also administer, keep your credentials on the hub as secrets instead: each run gets a lease of
just what it needs, and the hub takes it back when the run ends ([docs/credentials.md](credentials.md)). The
value goes in on stdin, or at a prompt that does not echo it, and never comes back out:

```sh
pbpaste | evo-agents hub secret set claude-oauth --kind env --env-var CLAUDE_CODE_OAUTH_TOKEN --project demo
evo-agents hub secret list
evo-agents hub run credentials 41 --project demo   # the leases run 41 got, given back or not; never their values
```

Several `env` secrets for the same variable bound to one project are accounts, such as two Claude subscriptions: each
run takes one of them by the rotation you set for the project (round-robin by default, weighted-round-robin or
fill-first) and keeps it, and a headless Claude Code run whose account hits its usage limit goes on in the same session
with the next one. `hub account login` adds a Claude account: it runs Anthropic's own `claude setup-token`, where you
approve in the browser, and puts the token it prints on the hub without showing it, ending a year from today less a
day. Run it again with the same name to renew the token, or use Add Claude account and Renew token on the web's
Accounts page. `hub account` shows and weighs the accounts, and `--account` picks one by hand
([docs/credentials.md](credentials.md), section "Accounts"):

```sh
evo-agents hub account login claude-b --project demo  # claude setup-token, then the token goes to the hub sealed
evo-agents hub account list                        # state, usage, runs and cost of each account; never a value
evo-agents hub account policy demo --strategy fill-first --cooldown 1800
evo-agents hub account pause claude-a
evo-agents hub run dispatch rollout 2 --account claude-b --project demo
```

## Running a hub

The hub is an API server, a job worker and a web interface, published as two images on GHCR for each release and run
with `deploy/hub/docker-compose.yml`, which can also build them from source, next to a Postgres database and a
Cloudflare R2 bucket. `evo-agents hub serve`
and `evo-agents hub worker` need the server extra: `pip install 'evo-ak[hub-server]'`. [docs/hub.md](hub.md)
covers the API and its OpenAPI document, sign-in from the CLI and the web, who sees what, the plan copies in git,
workers and runs, blobs on R2, the job worker and its queue, and operations: migrations, backup, restore and
health checks. [docs/plugins.md](plugins.md) covers plugins: their extension points, the plugin host and the
`plugin/1` protocol, pages and signing; [docs/hub-deployment.md](hub-deployment.md) a hub for a client, from the
same images, with its own branding, publishers and plugins. [docs/srs-plugin.md](srs-plugin.md) and
[docs/tasks-plugin.md](tasks-plugin.md) cover the core plugins srs (BA documents with versions, approval and
@ba) and tasks (a backlog linked to use cases and plan steps).

## The web

The hub's web interface (`web/`) is where people read the hub, answer decisions and dispatch runs. From 0.6.0 it
follows the evo-agents hub UI kit, in light and dark ([web/DESIGN.md](../web/DESIGN.md)):

- Home shows what waits on you, what runs, what ended lately and your workers, over every project you hold a grant
  on, in one request (`GET /v1/me/overview`). You answer a decision in a sheet over it and rerun a failed step from
  it.
- Every page takes the full width beside the sidebar.
- A run's page draws its phases on a timeline and reads its events as a Trace: the agent's messages, its thinking,
  and each tool call with its argument, exit code, duration and output. The raw log and the owner's terminal are
  tabs beside it, and a usage card adds up the run's tokens by type with the cost the agent reported. From 1280 px
  the session and the side column of cards scroll on their own. A repo links to its page on its forge, and on GitHub
  or GitLab its branch and commit do too.
- Monitor (`/monitor`) lists every run in flight over your projects and watches the ones you pick in a grid of live
  tiles that shares out the window: each tile a run's state, worker, time, plan steps and the tail of its trace, for
  viewing only. The tiles are in the URL (`?runs=project:id,...`), so a link opens the same grid.
- A plan's page shows its steps as Board, List or Graph (`?view=graph`): the graph draws each step under the steps it
  waits for, marks the Ready ones, and a step selected (`&step=11`) opens its details beside the graph, read only.
- Insights charts a project's runs by day over 7, 30 or 90 days: outcomes, failure rate, run time and tokens
  (`GET /v1/projects/{project}/runs/stats`), each chart with its table.
- Administration lists what needs a hub admin (`GET /v1/admin/overview`): tokens expiring or unused, failed graph
  builds, offline workers, people granted a role who have not signed in yet.
- Cmd K or Ctrl K opens a command palette that jumps to runs, plans and workers and offers the actions your grants
  allow. `?` lists the keyboard shortcuts: G then H, I, P, R or W to move, D to dispatch, / to search.
- Under 768 px tables become lists, filters move into a sheet, and a decision is a screen of its own.
- Each project's Curator page shows the night shift, its proposals with their evidence, and the charter.

## The Curator

The Curator is a project's night shift on the hub. Inside the hours its charter sets, the hub queues runs on one
member's worker without anybody pressing Run: a review run that reads the project's sessions, runs and code and
proposes changes with evidence, a Builder that makes an accepted change on a branch `curator/...`, and a Judge that
checks the pull request without reading the Builder's transcript. The hub computes each change's tier, merges only
tier 0 and only when the charter, CI, the Judge and the repo's ruleset all allow it, and sends the owner a morning
brief on the web and Telegram:

```sh
evo-agents hub curator charter set charter.yaml --project demo   # an admin of the project
evo-agents hub curator status --project demo
evo-agents hub curator proposal list --state open --project demo
evo-agents hub curator pause --project demo
```

[docs/curator.md](curator.md) describes the charter, the night shift and its caps, the tiers, the three roles,
the Curator's own GitHub App and the ruleset it needs, and Telegram.

## Running steps on your machine

A member's laptop or desktop can run plan steps the hub hands it, with that member's own coding agent:

```sh
uv tool install 'evo-ak[worker]'
evo-agents worker join --url https://hub.example.org --code XXXX-XXXX   # the code from the web's Workers page
evo-agents worker run                                                    # the daemon, in the foreground
```

The daemon claims runs, checks each run's repos, credentials and the programs its verify calls before the agent
starts, works in a git worktree of its own under `~/.evo/worker`, runs the agent's verify commands again, and pushes
the plan's branch, never forced. A run of one step never pushes the default branch; a plan run pushes it only when the
plan names it, putting its own commits on top when someone pushed meanwhile, or on `evo-run/<run>` when they do not
fit. On macOS the daemon keeps the machine awake while it holds a run, unless it is a laptop with its lid closed on
battery. [docs/workers.md](workers.md) covers the protocol and the daemon.
