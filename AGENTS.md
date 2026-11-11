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
- Keep project-specific data, internal hosts and credentials out of this public repository. That covers code,
  comments, tests, fixtures, docs, commit messages, PR titles and bodies, review comments and release notes: never
  name a member's or customer's project, its hosts, domains, groups or repos. Write fictional names instead
  (`northwind`, `lumen-*`, `lumenops.example`); hub projects that are part of evo-agents itself may be named. Before
  committing or opening a PR, read the diff, the message and the PR body for such names. The core plugin guard
  refuses them in runs and sessions too ([docs/guard.md](docs/guard.md)); a refusal names what to rewrite.
- Use plain language in documentation. Do not use the middle-dot character as a separator.

## Tests

Development speed comes first; a bug that reaches main is fixed forward. The only CI is the release tag's, and it
guards the PyPI wheel.

- Add a test only for behavior that can break silently and that no existing test reaches: parsing, state transitions,
  access rules, migrations, data that could be lost. Extend an existing test before writing a new one.
- Do not pin what a change sets on purpose: a tuple of names, a count of tables or rows, a whole response or health
  dict, help text, docs wording or UI copy. Assert the keys and the behavior a caller depends on.
- No new end-to-end spec for a UI change unless the flow cannot be reached any other way.
- Run the tests of what you changed, by node id. Delete a test that only restated what you changed on purpose.
