# Agent workflow and integration

For agents working in a project that uses evo-agents. Installation for people is in
[Getting started](getting-started.md#connect-your-coding-agent).

## Working on a project

- Read the project instructions first. Use `memory_search` and `memory_get` for earlier decisions and context.
- Use `kg_search` or `kg_context` to find relevant code and documents. Read the current source before relying on a
  graph snapshot. Before changing a file or symbol, use `kg_impact` and check the dependents and coverage it reports.
- Read a hub-managed plan with `plan_show`. Update its steps through `plan_step` or the hub CLI, with verification
  evidence. Files under `plans/` are read-only mirrors when the harness names `hub.project`.
- In a worker run, follow that run's instructions for reporting steps and decisions. Run-specific commands need
  the worker context; do not use them from an unrelated local session.
- Follow the run's autonomy policy. Keep credential values out of messages, logs and commits.

## Knowledge graph tools and hooks

```sh
uv tool install evo-ak                                 # the CLI the plugin runs
claude plugin marketplace add https://github.com/maycuatroi1/evo-agents
claude plugin install evo-kg@evo-agents
```

The plugin registers the MCP server `evo-kg`, a `using-project-graph` skill, and three hooks. The
server binds to the project of the session directory; pass `--project` in `.mcp.json` to pin one, or
run `evo-agents kg bind --project NAME DIR` once to tie a directory and everything below it to a project
(`kg bind --list` and `kg bind --remove DIR` manage those bindings). When the machine is signed in to a
hub that has a graph of the project, the server answers from the hub with the same tools
(`kg serve --backend auto`, the default; `local` and `hub` force one or the other). Inside a run of a worker,
`auto` and `hub` ask the hub with the run's worker token, signed in or not, and only `local` reads a graph built on
the machine ([docs/credentials.md](credentials.md)).

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
names a seed with more incoming edges than it reads (2,000), with their number. Its text lists the first 20 dependents
of each hop and counts the rest (`limit` raises it). [docs/kg.md](kg.md) has the coverage table per language, the
fields of the result, and how the text of every tool is written.

Hooks:

- SessionStart prints a short note: the bound project, the snapshot age, and how many git sources read a
  commit that is current with its remote's default branch, naming only the others with the commit read and
  how far behind it is now (`commits read: 16 sources, 13 current; behind: app 1550c7a (3 behind
  origin/main)`, or `commits read: 16 sources, all current`), counted from the checkout without the network.
- PostToolUse, after every evo-kg call, joins the labels of what the result revealed into a session
  label under `~/.evo/kg/sessions/`. It only rises; `kg_status` shows it. In a session without evo-kg,
  `evo-agents hub mcp` joins the labels of the `kg_*` results the hub serves into the same label.
- PostToolUse, after Grep, or `rg` or `grep` at the head of a Bash command, adds a note beside the
  result when a matched line falls in a symbol or file of the graph: at most four lines after its
  header, each naming by id who calls it (or imports the file), its tests and the plan steps that
  name it. It never changes, cuts or blocks the result, and says nothing when there is nothing to
  add. Set `EVO_KG_GREP_HINTS=0` to turn it off. [docs/kg.md](kg.md) says how it places a
  matched line.

The plugin runs the `evo-agents` CLI from the `PATH` (`uv tool install evo-ak`; [getting started](getting-started.md)
has the extras and the nightly builds), so it follows whatever release the machine installed and names none itself:
`.mcp.json` starts `evo-agents kg serve --sink claude-code@anthropic`, and each hook runs `evo-agents kg hook ... ||
true`. A hook never fails, blocks or holds the session: with `evo-agents` missing, or an older one that refuses the
subcommand, the shell's or the CLI's line goes to stderr and the hook exits 0. With no `evo-agents`, the server does
not start.
The server is marked `alwaysLoad`, so Claude Code loads its seven tools with the session instead of deferring them
behind ToolSearch: a session's first search no longer waits a turn for the tool to be loaded. The evo-hub server's
many tools stay deferred.

## The team hub

```sh
evo-agents hub login --url https://hub.example.org   # once per machine; the token stays in ~/.evo/hub
claude plugin install evo-hub@evo-agents
```

The `evo-hub` plugin registers the MCP server `evo-hub` (`evo-agents hub mcp`, which carries the session to the
hub's `/mcp`), a `using-agent-hub` skill, and six hooks. Its tools include the seven `kg_*` tools and `memory_search`,
`memory_get`, `memory_write`, `plan_list`, `plan_show`, `plan_handoff`, `plan_step`, `plan_complete`, `skill_list`,
`hub_projects` and `run_tool_stats`. A session that has the evo-kg plugin too gets the `kg_*` tools and the graph's instructions from
evo-kg alone: evo-hub leaves them out (it reads `enabledPlugins` of Claude Code's settings). In a harness
whose `harness.yaml` names `hub.project`, plans live on the hub and the files under `plans/` are read-only copies:
mark a step with `plan_step`, `evo-agents hub plan step` or `evo harness step`, never by editing the YAML. To hand a
plan to a new session or a subagent, give it `plan_handoff` (or `evo-agents hub plan handoff PLAN --step KEY`) rather
than your history: the plan's goal, each step done with its commits and `Decision:` lines, and the steps left, within a
token budget.

Hooks (`evo-agents hub hook session-start|stop|post-tool-use|user-prompt|local-tool|session-end`, same pins and `|| true` as evo-kg):

- SessionStart pulls the hub's memories of the session directory into Claude Code's memory directory, writes the
  plan copies of its harness (no commit; a copy edited by hand is left as it is and named), counts the skills
  `evo-agents hub skills sync` would change, and prints one line: hub, project, memories pulled, skills to sync.
- Stop pushes the memory files that changed since the last sync, so the next session on another machine has them.
  A turn that wrote no memory sends nothing; when the hub does not answer, the files wait and a later Stop pushes
  them (state in `~/.evo/hub/memory-state.json`).
- Stop also pushes the digest of a session of 6 messages or more to the project of its directory: calls and errors
  per tool and per Bash program, what the person wrote, model and tokens, the largest context one model call read
  (`peak_context_tokens`), the compactions, the characters of each tool's results, and how many results the spool
  below cut, their size before the cut and the reads of spool files, with every string that looks like a secret
  replaced on the machine first. A digest the hub did not take waits for a later Stop
  (`~/.evo/hub/digest-state.json`); the session of a worker run pushes none. Claude Code runs no Stop for a turn
  you interrupt, so SessionEnd pushes the digest too, and SessionStart finds the sessions of the last 7 days that
  neither hook reached (Claude Code killed, the machine off) and queues their digests for the next Stop.
- PostToolUse writes a result of Bash, Grep, WebFetch or an MCP tool longer than 10,000 characters
  (`EVO_SPOOL_THRESHOLD`), and a Read longer than 40,000 (`EVO_SPOOL_READ_THRESHOLD`), to
  `~/.evo/tool-output/<session>/` (mode 0600, kept 7 days, never sent to the hub) and gives the model its head, its
  tail, its size and the file's path instead; the tools of evo-hub and evo-kg, which cut their own text, and a plan's
  handoff pass whole.

- UserPromptSubmit, a second PostToolUse hook (`local-tool`), Stop and SessionEnd put a local Claude Code session
  of a hub project on the hub as a run of kind `local` (shown as local in the runs list): `running` while the agent
  works, `idle` while it waits for you, `ended` at session end, with its last activity, the plan and step it marks
  (`evo harness step`, `hub plan step` and `plan_step` name the run), and a live log. The log holds the agent's words,
  and each tool's name, its input (300 characters) and its result (400 characters), with every string that looks like a
  secret replaced on the machine first (the digest's redaction). The run also carries the session id, directory,
  machine, the first line of your first prompt (120 characters), and the Claude account: for the Code tab of Claude
  Desktop the email of the session record plus the claude-profiles profile name, for the CLI `oauthAccount.emailAddress`
  of the Claude Code configuration, else `unknown`. `EVO_HUB_LOCAL_SYNC=0` turns all of this off (Stop still pushes the
  digest and the memories). A session outside a hub project, and the agent of a worker run (`EVO_RUN_ID`), sends
  nothing. When the hub does not answer, events wait in `~/.evo/hub/local/<session>.json` (500 at most, kept 7 days)
  and a later hook sends them. [Unverified] The layout of Desktop's session record and of `profiles.json` follows
  claude-profiles' README and was not checked on a Mac; `EVO_CLAUDE_DESKTOP_DIRS` (`name=path`, separated like PATH)
  names the data folders when the defaults miss, and a record that cannot be read gives `unknown`.

All of them exit 0 whatever happens and give up after a few seconds. SessionStart and Stop print at most one line without
tokens or memory text and send nothing when the machine is not signed in; PostToolUse prints the shortened result or
nothing.

Codex takes the same plugin (`codex plugin marketplace add https://github.com/maycuatroi1/evo-agents`, then
`codex plugin add evo-hub@evo-agents`). Once you trust its hooks, which the first interactive session that loads them
asks, SessionStart and Stop do in a Codex session what they do in Claude Code, and the digest is built from Codex's
rollout. PostToolUse leaves Codex's output as it is: Codex ignores a replaced tool output and cuts a long one to its
own `tool_output_token_limit` ([docs/hub.md](hub.md), Codex).

## Where an agent's runs go

Nobody picks the machine of a run. Each agent has a pool of its own, `agent-<handle>`, and the agent runs everything it
does (steps, plan runs, author runs, chat, tasks, triage, judge, loop) on a ready node of that pool, whoever dispatches
it. `hub run dispatch|plan|author|rerun` and `worker triage rerun` therefore have no `--worker`, `--pool`, `--label` or
`--tolerate`, and the web dispatch, plan run and author run dialogs have no worker field. An admin of the project sees
the pool, its nodes and the account pool of an agent on the agent's page of the web. An agent whose pool has no ready
node keeps its runs queued, and the admins hear of it once, until someone adds a node. Model accounts come from the agent's account pool alone, never from the dispatcher or the login stored on a node. See
[docs/hub.md](hub.md) for the pool and its placement, [docs/workers.md](workers.md) for the nodes of several agents on
one machine (`bootstrap.sh` with `EVO_AGENT_TOKENS`), and [docs/credentials.md](credentials.md) for account pools.
