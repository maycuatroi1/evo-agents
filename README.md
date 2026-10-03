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
uv tool install git+https://github.com/maycuatroi1/evo-agents
# or, from a checkout: python -m pip install -e '.[test]'
```

The core needs Python 3.10+ and PyYAML; nothing else.

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

Then, from the harness:

```sh
evo-agents harness validate          # schema and reference checks for every harness file
evo-agents kg sync --build           # run connectors, build the graph
evo-agents kg status                 # freshness, coverage and held deletions per source
evo-agents kg build --verify         # rebuild from scratch and compare hashes
evo-agents kg query kg_search "KB-01"
```

Corpus and graph live in `~/.evo/kg/<project>/` (override with `EVO_KG_HOME`), never in a repo.

## Using it from Claude Code

```sh
claude plugin marketplace add https://github.com/maycuatroi1/evo-agents
claude plugin install evo-kg@evo-agents
```

The plugin registers the MCP server `evo-kg` (`kg_search`, `kg_context`, `kg_node`, `kg_status`,
`kg_more`), a `using-project-graph` skill, and a SessionStart note. The server binds to the project
of the session directory; pass `--project` in `.mcp.json` to pin one, or run
`evo-agents kg bind --project NAME DIR` once to tie a directory and everything below it to a project
(`kg bind --list` and `kg bind --remove DIR` manage those bindings).

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
plugins/      Claude Code marketplace (plugin evo-kg)
```

## License

Apache-2.0. See [LICENSE](LICENSE).
