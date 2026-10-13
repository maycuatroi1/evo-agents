# evo-agents

Shared tooling for agent harnesses: a cluster of repos plus the manifest, plans and documents that
tell a coding agent how the cluster fits together.

The first piece is a multi-source knowledge graph. Connectors read git repos, the harness itself,
wikis and project CLIs, speak one JSON Lines protocol (`kg/1`), and feed an append-only log per
project. A deterministic pipeline turns that log into a SQLite graph that agents query over MCP.
Every node and edge carries provenance and a sensitivity label, and a session only sees what its
clearance allows.

Status: early prototype. Interfaces will change.

## Install

```sh
uv tool install 'evo-ak[graphify]'   # or: pip install 'evo-ak[graphify]' (PyPI name; the command is evo-agents)
uv tool install 'evo-ak[graphify] @ git+https://github.com/maycuatroi1/evo-agents'   # unreleased main
# or, from a checkout: python -m pip install -e '.[test,graphify]'
```

The core needs Python 3.11+ and PyYAML; nothing else. The `graphify` extra adds graphify's tree-sitter
extractors, which give code symbols for about 40 languages; without it only Python files get symbols,
and `kg build` and `kg status` warn with the number of code files left without them. A source that sets
`code: {backend: graphify-ast}` fails the build instead when the extra is missing.

macOS, Linux and Windows run every command but `evo-agents worker`, whose daemon needs process groups, tmux and
launchd or systemd: it runs on macOS and Linux, and on Windows inside WSL. On Windows, `kg schedule install` prints the
command to give Task Scheduler instead of loading a LaunchAgent, and a skill published from there marks a file
executable when it starts with `#!`, since Windows keeps no execute bit.

## Quick start

A harness opts in with a `knowledge.yaml` next to its `harness.yaml`:

```yaml
version: 1
project: demo
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - id: claude-code@anthropic
      kind: agent-session
      clearance: {level: internal}
sources:
  - id: harness            # plans, contracts, bindings, docs of the harness itself
    connector: harness
    label: {level: internal, integrity: U}
  - id: app                # a repo declared in harness.yaml
    connector: git
    repo: app
    refresh: 1h            # due for kg sync --due once an hour
    label: {level: internal, integrity: U}
  - id: wiki               # any command that speaks kg/1
    connector: exec
    command: [my-cli, kg-connector, wiki]
    credentials: [{key: my.wiki.token, env: WIKI_TOKEN}]
    label: {level: customer, integrity: U}
identifiers:
  - kind: UseCase
    pattern: '\bKB-\d{2}\b'
```

A git source reads the text of markdown, YAML, text, config and code files (or of what its `include:`
globs match) and records other files by path only. It skips the paths its `exclude:` globs match, plus a
default list: build output, lock files, virtualenvs and agent folders such as `.claude/` and `.agents/`,
at any depth, and `state/` at the root only (a nested `src/state/` is ordinary code).
`allow:` takes globs that bring paths from that default list back, for example
`allow: [".claude/CLAUDE.md"]`, or `"*/.claude/CLAUDE.md"` for a nested one; `exclude:` still wins over
`allow:`. Only the git connector reads `allow:`.
`fetch: true` on a git source whose `ref:` is `origin/<branch>` fetches that branch from `origin` (with a
timeout) before each run, so the graph follows the remote without anyone fetching the clone. A failed
fetch keeps the run ok: it logs a warning and reads the ref as the clone has it. Without the key nothing
is fetched.
Before a file enters the corpus, the git connector replaces each secret in its text with a marker naming
its kind: a private key in PEM (`[redacted:pem]`), a provider's key (Anthropic, OpenAI, AWS, Slack, Google,
GitHub, hub tokens), the password of a URI, and the literal value of a variable or key named as a password,
secret, token, API key or private key (`DB_PASSWORD: [redacted:password]`). References such as `${VAR}`,
`os.environ[...]` or `process.env.X`, empty values, placeholders and descriptive words stay, and so does the
rest of the file, so its structure still enters the graph. The patterns favour precision over recall;
`evo_agents/kg/redact.py` lists what they take and leave.

A markdown file whose frontmatter `id:` matches an identifier pattern is where that code is defined;
a copy says `derived_from:` in its frontmatter, and `kg status` lists codes defined in more than one place.

Then, from the harness:

```sh
evo-agents harness validate          # schema and reference checks for every harness file
evo-agents kg sync --build           # run connectors, build the graph
evo-agents kg status                 # freshness, coverage and held deletions per source
evo-agents kg build --verify         # rebuild from scratch and compare hashes
evo-agents kg query kg_search "KB-01"
evo-agents kg query kg_impact --arg 'paths=["app:src/search.py"]'
```

Corpus and graph live in `~/.evo/kg/<project>/` (override with `EVO_KG_HOME`), never in a repo. Every
source sync and every build appends one line to `audit.jsonl` there: ids, times, counts and versions,
never item content. A sync's line also counts the secrets the git connector redacted, by item id and kind,
never their values.

A corpus synced before its git connector redacted keeps the old copies: in the blobs of earlier runs, in memo rows,
and in free pages of the graph. `evo-agents kg scrub` (`--all` for every project directory of the kg home) reads
them with the connector's patterns and lists each blob that holds a secret by hash, path, item id and kind of
secret, never the value. A blob is old when nothing uses it, live when an item of the corpus refers to it now (a
sync of its source replaces it with a redacted copy, so sync and build first), and kept when only records the corpus
no longer holds name it, in the log of a run still in use (an item or a tombstone of that run still wins). Counting
changes nothing. `--purge` says what it is about to delete and asks first (`--yes` when no terminal can answer). It
then rewrites each run log in use that names a kept blob, keeping of its item and tombstone records only those the
corpus holds (the log keeps its name and format, and replays to the same state), deletes the old and kept blobs, the
run logs of old runs that name them and the memo rows holding a secret, compacts the memo and the graph so no free
page keeps the text, and appends a `scrub` line with the counts to `audit.jsonl`. It never deletes what the corpus
uses, and it holds every source's lock meanwhile, so a sync started then stops at once instead of writing.
`hub kg push` never sends a run the hub already holds, so a rewritten log leaves the machine only for a run the hub
lacks. In order:

```sh
evo-agents kg sync --build                   # live copies become old ones
evo-agents kg scrub                          # count, change nothing
evo-agents kg scrub --all --purge            # delete the old copies of every project, after a yes
```

To keep graphs fresh, give sources a `refresh` interval (`30m`, `6h`, `1d`). `kg sync --due` runs only
the sources whose interval has passed since their last ok run; `--all` does that for every project that
has synced on this machine. On macOS, `kg schedule install` loads a LaunchAgent that runs
`kg sync --due --all --build` every hour (`kg schedule print` shows the plist, `kg schedule uninstall`
removes it; output goes to `~/.evo/kg/schedule.log`). On a machine signed in to a hub, the command also
gets `--push`, which sends the runs the hub lacks (`evo-agents hub kg push` does the same by hand).

## Using it from Claude Code

```sh
claude plugin marketplace add https://github.com/maycuatroi1/evo-agents
claude plugin install evo-kg@evo-agents
```

The plugin registers the MCP server `evo-kg`, a `using-project-graph` skill, and three hooks. The
server binds to the project of the session directory; pass `--project` in `.mcp.json` to pin one, or
run `evo-agents kg bind --project NAME DIR` once to tie a directory and everything below it to a project
(`kg bind --list` and `kg bind --remove DIR` manage those bindings). When the machine is signed in to a
hub that has a graph of the project, the server answers from the hub with the same tools
(`kg serve --backend auto`, the default; `local` and `hub` force one or the other).

| Tool | Use |
|---|---|
| `kg_search` | find ids by name, code, path or words |
| `kg_context` | connected subgraph around a question or ids, within a token budget |
| `kg_node` | one node: properties, evidence, edges |
| `kg_path` | shortest path between two ids, or the trace chains from one (requirement, step, code, test) |
| `kg_impact` | what depends on ids, `repo:path` files or a unified diff, before you change them |
| `kg_status` | bound project, clearance, snapshot age, coverage per source, session label |
| `kg_more` | continue a truncated result |

`kg_impact` walks back over callers, in other files too when the caller's imports say where the callee lives,
importers, members, definers, subclasses (`inherits`), overriding methods (`overrides`), implementations and tests.
Each result notes, per language of its seeds, whether calls across files, inheritance and overrides are covered, and
names a seed with more incoming edges than it reads (2,000), with their number. [docs/kg.md](docs/kg.md) has the
coverage table per language and the fields of the result.

Hooks:

- SessionStart prints a short note: the bound project and the snapshot age.
- PostToolUse, after every evo-kg call, joins the labels of what the result revealed into a session
  label under `~/.evo/kg/sessions/`. It only rises; `kg_status` shows it.
- PreToolUse, before Grep, Glob, or `rg` or `grep` in Bash, may add a note with the ids of graph
  nodes named like the search. It never allows or blocks the call. Set `EVO_KG_GREP_HINTS=0` to turn it
  off.

The plugin needs [uv](https://docs.astral.sh/uv/) on `PATH` and pins the release it runs, the version in its
`plugin.json`: the server starts with `uvx --from evo-ak==<version> evo-agents`, which downloads and caches that version
on first start. The hooks run `uvx --offline --from evo-ak==<version> evo-agents`, so they never wait on the network;
they stay silent when `uvx` is missing or until the server has cached the package.

### The team hub

```sh
evo-agents hub login --url https://hub.example.org   # once per machine; the token stays in ~/.evo/hub
claude plugin install evo-hub@evo-agents
```

The `evo-hub` plugin registers the MCP server `evo-hub` (`evo-agents hub mcp`, which carries the session to the
hub's `/mcp`), a `using-agent-hub` skill, and three hooks. Its tools are the seven `kg_*` tools and `memory_search`,
`memory_get`, `memory_write`, `plan_list`, `plan_show`, `plan_handoff`, `plan_step`, `skill_list`, `hub_projects` and
`run_tool_stats`. In a harness
whose `harness.yaml` names `hub.project`, plans live on the hub and the files under `plans/` are read-only copies:
mark a step with `plan_step`, `evo-agents hub plan step` or `evo harness step`, never by editing the YAML. To hand a
plan to a new session or a subagent, give it `plan_handoff` (or `evo-agents hub plan handoff PLAN --step KEY`) rather
than your history: the plan's goal, each step done with its commits and `Decision:` lines, and the steps left, within a
token budget.

Hooks (`evo-agents hub hook session-start|stop|post-tool-use`, same pins and `|| true` as evo-kg):

- SessionStart pulls the hub's memories of the session directory into Claude Code's memory directory, writes the
  plan copies of its harness (no commit; a copy edited by hand is left as it is and named), counts the skills
  `evo-agents hub skills sync` would change, and prints one line: hub, project, memories pulled, skills to sync.
- Stop pushes the memory files that changed since the last sync, so the next session on another machine has them.
  A turn that wrote no memory sends nothing; when the hub does not answer, the files wait and a later Stop pushes
  them (state in `~/.evo/hub/memory-state.json`).
- Stop also pushes the digest of a session of 6 messages or more to the project of its directory: calls and errors
  per tool and per Bash program, what the person wrote, model and tokens, the largest context one model call read
  (`peak_context_tokens`), the compactions and the characters of each tool's results, with every string that looks
  like a secret replaced on the machine first. A digest the hub did not take waits for a later Stop
  (`~/.evo/hub/digest-state.json`); the session of a worker run pushes none.
- PostToolUse writes a result of Bash, Read, Grep or an MCP tool longer than 10,000 characters (`EVO_SPOOL_THRESHOLD`)
  to `~/.evo/tool-output/<session>/` (mode 0600, kept 7 days, never sent to the hub) and gives the model its head, its
  tail, its size and the file's path instead.

All three exit 0 whatever happens and give up after a few seconds. SessionStart and Stop print at most one line without
tokens or memory text and send nothing when the machine is not signed in; PostToolUse prints the shortened result or
nothing.

Codex takes the same plugin (`codex plugin marketplace add https://github.com/maycuatroi1/evo-agents`, then
`codex plugin add evo-hub@evo-agents`). Once you trust its hooks, which the first interactive session that loads them
asks, SessionStart and Stop do in a Codex session what they do in Claude Code, and the digest is built from Codex's
rollout. PostToolUse leaves Codex's output as it is: Codex ignores a replaced tool output and cuts a long one to its
own `tool_output_token_limit` ([docs/hub.md](docs/hub.md), Codex).

### Workers

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

Then, from any machine signed in to the hub:

```sh
evo-agents hub run dispatch rollout 2 --project demo
evo-agents hub run logs 41 --follow --project demo
evo-agents hub run approve 41 --project demo
```

A worker can also take a whole plan as one plan run: its agent does every step not done yet in the order
`depends_on` allows, in a worktree of each repo those steps name, and reports each step as it goes, verified, committed
and pushed on the plan's branch. It stops for you only on a decision it may not take alone (a deploy, deleting data,
a migration on real data, sending outside, spending money, or an architectural or scope choice the plan leaves open),
which waits in the Inbox on the web or on the command line:

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
(`EVO_WORKER_CLAUDE_CODE_AUTO_COMPACT_WINDOW` on the worker sets another window).

The agent on a worker runs with the full rights of the machine's owner, and only that owner dispatches runs to it.
[docs/workers.md](docs/workers.md) describes the protocol and plan runs, [docs/hub.md](docs/hub.md) every `hub run`
and `hub decision` command, and [docs/notifications.md](docs/notifications.md) decisions, notices and notifications.

On a machine others also administer, keep your credentials on the hub as secrets instead: each run gets a lease of
just what it needs, and the hub takes it back when the run ends ([docs/credentials.md](docs/credentials.md)). The
value goes in on stdin, or at a prompt that does not echo it, and never comes back out:

```sh
pbpaste | evo-agents hub secret set claude-oauth --kind env --env-var CLAUDE_CODE_OAUTH_TOKEN --project demo
evo-agents hub secret list
evo-agents hub run credentials 41 --project demo   # the leases run 41 got, given back or not; never their values
```

### Running a hub

The hub is an API server, a job worker and a web interface, published as two images on GHCR for each release and run
with `deploy/hub/docker-compose.yml` next to a Postgres database and a Cloudflare R2 bucket. `evo-agents hub serve`
and `evo-agents hub worker` need the server extra: `pip install 'evo-ak[hub-server]'`. [docs/hub.md](docs/hub.md)
covers the API and its OpenAPI document, sign-in from the CLI and the web, who sees what, the plan copies in git,
workers and runs, blobs on R2, the job worker and its queue, and operations: migrations, backup, restore and
health checks.

### The web

The hub's web interface (`web/`) is where people read the hub, answer decisions and dispatch runs. From 0.6.0 it
follows the evo-agents hub UI kit, in light and dark ([web/DESIGN.md](web/DESIGN.md)):

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
- Insights charts a project's runs by day over 7, 30 or 90 days: outcomes, failure rate, run time and tokens
  (`GET /v1/projects/{project}/runs/stats`), each chart with its table.
- Administration lists what needs a hub admin (`GET /v1/admin/overview`): tokens expiring or unused, failed graph
  builds, offline workers, people granted a role who have not signed in yet.
- Cmd K or Ctrl K opens a command palette that jumps to runs, plans and workers and offers the actions your grants
  allow. `?` lists the keyboard shortcuts: G then H, I, P, R or W to move, D to dispatch, / to search.
- Under 768 px tables become lists, filters move into a sheet, and a decision is a screen of its own.
- Each project's Curator page shows the night shift, its proposals with their evidence, and the charter.

### The Curator

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

[docs/curator.md](docs/curator.md) describes the charter, the night shift and its caps, the tiers, the three roles,
the Curator's own GitHub App and the ruleset it needs, and Telegram.

### Running steps on your machine

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
battery. [docs/workers.md](docs/workers.md) covers the protocol and the daemon.

## Writing a connector

A connector is a generator in process or any executable. It writes JSON objects, one per line:
`hello` first, then `item`, `tombstone`, `listing`, `state`, `error`, `log`, and `closed` last.
Item IDs are `<source>:<kind>:<native key>`; every item carries a revision and a content hash over
canonical JSON. Deletions are only inferred from a scoped `listing` that ends with
`complete: true`, so a connector that dies half-way never deletes anything.

```sh
evo-agents kg connector test -- my-cli kg-connector wiki --fixture tests/fixtures/wiki
```

checks protocol, determinism, replay and permutation, truncated listings, golden output and labels.

## Layout

```
evo_agents/
  harness/    schema and loader for harness.yaml, knowledge.yaml, contracts.yaml, plans
  kg/         protocol, connectors, corpus, pipeline, store, policy, MCP server
  hub/        team hub: server, client commands, MCP proxy, plugin hooks
  worker/     the worker daemon, `evo-agents worker`
plugins/      Claude Code marketplace (plugins evo-kg and evo-hub)
web/          the hub's web interface (Next.js, its own image)
deploy/hub/   the hub's Dockerfile and compose files
scripts/      bump_version.py X.Y.Z: the release version in evo_agents/__init__.py and in each plugin's manifest and pins
docs/         hub.md: running and using the hub; workers.md: workers, runs, plan runs and the daemon;
              notifications.md: decisions, notices and notifications; credentials.md: secrets and the leases of
              worker runs; curator.md: the Curator, a project's night shift; structure.md: package boundaries
              and size budgets that CI checks, and how to split a module; kg.md: what kg_impact follows, and
              its coverage per language
```

## License

Apache-2.0. See [LICENSE](LICENSE).
