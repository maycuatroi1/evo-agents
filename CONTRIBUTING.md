# Contributing

Bug reports, documentation fixes and code changes are welcome. For a bug, include the command you ran, the
version, what you expected and the error output. Remove credentials and private project data before posting an
[issue](https://github.com/maycuatroi1/evo-agents/issues).

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

Pull requests and pushes to main do not run the full CI suite. It runs for release tags or manual dispatch;
local verification belongs in the pull request description. See [structure checks](docs/structure.md) for package
boundaries and size limits.

## Repository layout

| Path | Contents |
| --- | --- |
| `evo_agents/harness/` | Project manifests, schemas and validation |
| `evo_agents/kg/` | Connectors, graph storage, retrieval and MCP tools |
| `evo_agents/hub/` | Hub API, CLI, jobs and agent integration |
| `evo_agents/worker/` | Worker daemon and coding-agent runtimes |
| `plugins/` | Coding-agent plugins |
| `web/` | Next.js web interface |
| `deploy/` | Hub and worker deployment files |
| `tests/` | Python tests and fixtures |
| `docs/` | Setup guides and technical references |

For a new data source, see [Writing a connector](docs/kg-setup.md#writing-a-connector).
For hub extensions, see [Plugins](docs/plugins.md). Release instructions are in [Releases](docs/releases.md).
Agent-specific instructions start in [AGENTS.md](AGENTS.md).
