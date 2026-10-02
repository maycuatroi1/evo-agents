# evo-agents

Shared tooling for agent harnesses: a cluster of repos plus the manifest, plans and
documents that tell a coding agent how the cluster fits together.

The first piece is a multi-source knowledge graph. Connectors read git repos, wikis
and project CLIs, speak one JSON Lines protocol (`kg/1`), and feed an append-only
log per project. A deterministic pipeline turns that log into a SQLite graph that
agents query over MCP. Every node and edge carries provenance and a sensitivity
label, and a session only sees what its clearance allows.

Status: early prototype. Interfaces will change.

## Layout

```
evo_agents/
  harness/    schema and loader for harness.yaml, knowledge.yaml, contracts.yaml, plans
  kg/         protocol, connectors, corpus, pipeline, store, policy, MCP server
plugins/      Claude Code marketplace (plugin evo-kg)
```

## License

Apache-2.0. See [LICENSE](LICENSE).
