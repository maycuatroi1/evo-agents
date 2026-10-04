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

The core needs Python 3.10+ and PyYAML; nothing else. The `graphify` extra adds graphify's tree-sitter
extractors, which give code symbols for about 40 languages; without it only Python files get symbols,
and `kg build` and `kg status` warn with the number of code files left without them. A source that sets
`code: {backend: graphify-ast}` fails the build instead when the extra is missing.

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
default list: build output, lock files, virtualenvs and agent folders such as `.claude/` and `.agents/`.
`allow:` takes globs that bring paths from that default list back, for example
`allow: [".claude/CLAUDE.md"]`; `exclude:` still wins over `allow:`. Only the git connector reads `allow:`.

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
never item content.

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

Hooks:

- SessionStart prints a short note: the bound project and the snapshot age.
- PostToolUse, after every evo-kg call, joins the labels of what the result revealed into a session
  label under `~/.evo/kg/sessions/`. It only rises; `kg_status` shows it.
- PreToolUse, before Grep, Glob, or `rg` or `grep` in Bash, may add a note with the ids of graph
  nodes named like the search. It never allows or blocks the call. Set `EVO_KG_GREP_HINTS=0` to turn it
  off.

The plugin needs [uv](https://docs.astral.sh/uv/) on `PATH` and pins the release it runs: the server
starts with `uvx --from evo-ak==0.1.2 evo-agents`, which downloads and caches that version on first start. The hooks
run `uvx --offline --from evo-ak==0.1.2 evo-agents`, so they never wait on the network; they stay silent when `uvx`
is missing or until the server has cached the package.

### The team hub

```sh
evo-agents hub login --url https://hub.example.org   # once per machine; the token stays in ~/.evo/hub
claude plugin install evo-hub@evo-agents
```

The `evo-hub` plugin registers the MCP server `evo-hub` (`evo-agents hub mcp`, which carries the session to the
hub's `/mcp`), a `using-agent-hub` skill, and two hooks. Its tools are the seven `kg_*` tools and `memory_search`,
`memory_get`, `memory_write`, `plan_list`, `plan_show`, `plan_step`, `skill_list` and `hub_projects`. In a harness
whose `harness.yaml` names `hub.project`, plans live on the hub and the files under `plans/` are read-only copies:
mark a step with `plan_step`, `evo-agents hub plan step` or `evo harness step`, never by editing the YAML.

Hooks (`evo-agents hub hook session-start|stop`, same pins and `|| true` as evo-kg):

- SessionStart pulls the hub's memories of the session directory into Claude Code's memory directory, writes the
  plan copies of its harness (no commit; a copy edited by hand is left as it is and named), counts the skills
  `evo-agents hub skills sync` would change, and prints one line: hub, project, memories pulled, skills to sync.
- Stop pushes the memory files that changed since the last sync, so the next session on another machine has them.
  A turn that wrote no memory sends nothing; when the hub does not answer, the files wait and a later Stop pushes
  them (state in `~/.evo/hub/memory-state.json`).

Both exit 0 whatever happens, give up after a few seconds, print at most one line without tokens or memory text, and
send nothing when the machine is not signed in.

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
plugins/      Claude Code marketplace (plugins evo-kg and evo-hub)
```

## License

Apache-2.0. See [LICENSE](LICENSE).
