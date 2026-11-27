# Contributing

Bug reports, documentation fixes and code changes are welcome. For a bug, include the command you ran, the
version, what you expected and the error output. Remove credentials and private project data before posting an
[issue](https://github.com/maycuatroi1/evo-agents/issues). Report a vulnerability privately instead, as
[SECURITY.md](SECURITY.md) describes.

## Development setup

Clone the repo, then install an editable copy with Python 3.11+:

```sh
git clone https://github.com/maycuatroi1/evo-agents.git
cd evo-agents
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test,graphify]'
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.

For hub and worker development, install the extra dependencies and the core plugins pinned by the hub image:

```sh
python -m pip install -e '.[test,graphify,hub-server,worker]'
python -m pip install --no-deps --require-hashes -r deploy/hub/core-plugins.txt
```

## Checks

Run the tests for the behavior you change before opening a pull request. The core suite can run without a hub:

```sh
python -m pytest -q
```

Hub database tests need a test Postgres instance and `EVO_HUB_TEST_DSN`; see
[tests/hub/pg.py](tests/hub/pg.py) for the test database setup. Tests that need unavailable optional dependencies or
services may skip. Web setup and tests are in [web/README.md](web/README.md).

For documentation changes, check the CLI examples as well as links and anchors:

```sh
python -m pytest -q tests/hub/test_contract.py::test_the_docs_of_this_repository_pass tests/kg/test_cross_repo_scope.py
```

Pull requests and pushes to main run no CI. A release tag runs the Python suite on Python 3.12 and an import of every
module on Python 3.11 before it publishes to PyPI; lint, Windows, the web and the hub's image run only in a CI run
started by hand with `extras`. Local verification belongs in the pull request description. See [structure checks](docs/structure.md) for package
boundaries and size limits.

## Repository layout

| Path | Contents |
| --- | --- |
| `evo_agents/cli.py` | The root `evo-agents` command: the `tree()` of every package's commands and the `harness` commands |
| `evo_agents/clikit/` | The command kit: `Command`, `ListCommand`, `Group`, `Result` and its views, `Arg` (standard library only) |
| `evo_agents/infra/` | Git, atomic and JSON files, `RetryPolicy` (standard library only) |
| `evo_agents/schema/` | The policy, ontology and canonical hash the harness and the graph share |
| `evo_agents/harness/` | Project manifests, schemas and validation |
| `evo_agents/kg/` | Connectors, build stages, graph storage, retrieval and MCP tools; its commands in `kg/cli/` |
| `evo_agents/plugins/` | What the hub and the worker share to read, trust and place a plugin; its commands in `plugins/cli/` |
| `evo_agents/hub/api/` | The gateway to the hub's API: the one place outside the server that writes a `/v1/...` path |
| `evo_agents/hub/cli/` | The `hub` commands, one module or package per command word |
| `evo_agents/hub/server/` | The hub's FastAPI app: one package per feature in layers (`routes`, `service`, `repository`, `models`) on `server/base/` |
| `evo_agents/hub/worker/` | The hub's background jobs: one `Job` class each, listed in `registry.py` |
| `evo_agents/hub/` | The hub's domain outside the server: tables, configuration, the clients, the graph sync |
| `evo_agents/worker/` | Worker daemon, kinds of run, coding-agent adapters and platforms; its commands in `worker/cli/` |
| `evo_agents/core_plugins/` | The core plugins the hub ships (`builder`, `browser`, `guard`), reached through their manifests |
| `plugins/` | Coding-agent plugins |
| `web/` | Next.js web interface |
| `deploy/` | Hub and worker deployment files |
| `tests/` | Python tests and fixtures |
| `docs/` | Setup guides and technical references |

Where a new class goes, and which base it takes, is in [Object design](docs/structure.md#object-design).

For a new data source, see [Writing a connector](docs/kg-setup.md#writing-a-connector).
For hub extensions, see [Plugins](docs/plugins.md). Release instructions are in [Releases](docs/releases.md).
Agent-specific instructions start in [AGENTS.md](AGENTS.md).
