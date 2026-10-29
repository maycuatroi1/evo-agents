# Working in evo-agents

The public README is for people deciding whether and how to use the project. Keep it focused on the product,
a concrete workflow and the first steps. Put command catalogs, protocols, hooks and storage internals in `docs/`.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, repository layout and checks.
- Read [docs/agents.md](docs/agents.md) for the agent workflow, MCP tools and hub-managed plans.
- Read the relevant reference before changing a subsystem: [graph](docs/kg.md), [hub](docs/hub.md),
  [workers](docs/workers.md), [plugins](docs/plugins.md), [package boundaries](docs/structure.md),
  or [web](web/README.md).
- Check live source against the graph snapshot before changing code. Keep documentation examples executable;
  when moving one, update its links and the tests that read it.
- Keep project-specific data, internal hosts and credentials out of this public repository.
- Use plain language in documentation. Do not use the middle-dot character as a separator.
