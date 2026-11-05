# The structure of the code

Two checks keep the structure of `evo_agents`: import-linter checks the boundaries between its packages, and the
tests marked `structure` in `tests/test_structure.py` check size and complexity budgets. basedpyright checks its types
against a baseline (section Type check). A plain `pytest` runs none of them (`addopts` deselects the marker); run them
when a change moves code between packages or grows a large module. A failure says what is over, by how much, and how
to fix it. What broke a rule when the rules came in (2026-10-10) was frozen as an exception, and exceptions only
shrink; since 2026-11-05 the baseline of the budgets holds none.

## In CI

The CI of a release tag keeps these checks. The full tier of `.github/workflows/ci.yml`, which `release.yml` runs on a
`v*` tag before it builds anything, has a `lint` job that runs, in the environment of a worker's run (the extras of
`[tool.evo-agents.worker]`):

```sh
ruff check .
ruff format --check .
lint-imports
git fetch origin +refs/heads/main:refs/remotes/origin/main
EVO_STRUCTURE_BASE=origin/main python -m pytest -q -m structure tests/test_structure.py
python -m basedpyright
```

A failure of any of them fails `ci-ok`, so the tag is not released. In the same run, the Linux test jobs set
`EVO_HUB_TEST_REQUIRE_DSN=1` next to `EVO_HUB_TEST_DSN`: with it set and the DSN missing, `tests/hub/pg.py` fails the
run instead of skipping every hub test (`tests/hub/test_require_dsn.py`). A pull request and a push to main run no CI,
so run the commands below before you merge a change that touches what they check.

## Commands

```sh
lint-imports                                  # package boundaries: [tool.importlinter] in pyproject.toml
python -m pytest -q -m structure tests/test_structure.py   # budgets, and the baseline and exceptions against origin/main
python -m tests.structure report              # modules and functions over budget, most over first, then most changed
python -m tests.structure report --json       # the same rows as JSON
python -m tests.structure tighten             # lower the baseline to the code once you have shrunk something
```

The comparison with main reads `origin/main`: run `git fetch origin` first, or it is skipped. With
`EVO_STRUCTURE_BASE=origin/main` set, a missing ref fails instead.

## Package boundaries

| Contract | Rule | Exceptions today |
| --- | --- | --- |
| `worker-not-hub-server` | nothing `evo_agents.worker` imports reaches `evo_agents.hub.server`, directly or through another module | the parser of `worker plan`, which borrows the hub CLI's JSON option and plan keys (2) |
| `kg-not-hub-or-worker` | `evo_agents.kg` imports neither `evo_agents.hub` nor `evo_agents.worker` | its CLI: `kg.cli`, `kg.cli_graph`, `kg.schedule` |
| `hub-server-from-hub-server` | outside `evo_agents.hub.server`, `evo_agents.hub` does not import it | `hub serve` and `hub openapi` (the app), `kg_prune` (audit), `hub worker` (ten job modules) |
| `plugins-standalone` | `evo_agents.plugins`, the code the hub and the worker share to read, trust and place a plugin, imports neither `cli`, `worker`, `hub`, `kg` nor `harness` | none |
| `packages-acyclic` | `cli`, `worker`, `hub`, `kg`, `harness`, `plugins` and `schema` import each other without cycles | the kg CLI, the MCP proxy's worker home, the CLI contract, the harness loader's use of kg (8) |
| `hub-runs-layers` | in `evo_agents.hub.server.runs`, `routes` imports `service` and `models`, `service` imports `models`, never the reverse; every module of the package is in one of the three | none |

Packages import downward: `cli`, then `worker`, `hub`, `kg`, `harness`, `plugins`, `schema`. import-linter reads imports inside
functions too, so a lazy import does not get around a contract. To fix a broken one, move the shared code down into
a package both may import (a type into `evo_agents.kg` or `evo_agents.schema`, the run protocol into
`evo_agents.hub.runs`), or have the upper package pass it down as an argument. Each contract prints its own advice.

Inside `evo_agents.hub.server.runs`, the run API, layers go one way too. A route handler passes the request's parts
to a public function of `service`, which checks, queries and moves runs with SQLAlchemy Core on
`evo_agents.hub.tables` and returns the models of `models`. Other modules import `service` and `models`, never
`routes`.

Each contract's `ignore_imports` only shrinks: the test fails a change that adds an entry main does not have. When
you remove the import an entry names, remove the entry too (import-linter fails on an entry that matches nothing).

## Budgets

| Budget | Limit | Measured by | Frozen in `tests/structure_baseline.json`, 2026-10-11 | Today |
| --- | --- | --- | --- | --- |
| module | 1,000 lines | lines of the file | 8 modules | none |
| function | 50 statements, complexity 15 | ruff PLR0915 and C901, `# noqa` ignored | 36 functions | none |
| private names | none imported from another module | `from module import _name` | 74 imports | none |

When the rules came in, the baseline held 10 modules, 40 functions and 101 imports. The plan code-quality-upgrade
(2026-11) split the rest, so the baseline is empty and every module, function and import keeps the budget, the
alembic revisions included. `python -m tests.structure report` prints anything over budget.

Only `evo_agents` is measured, not the tests. A frozen module or function may not grow past its number. Once it
shrinks, the test asks you to lower the number, and once it fits the budget, to drop the entry: `tighten` does both.
Against main, the baseline may add no entry and raise no number. A function moved unchanged to another module is not
an addition; `tighten` moves its entry. Never raise a number to make the test pass: split the code.

## Type check

basedpyright checks the types of `evo_agents` in standard mode. It is pinned in the `test` extra (a new release finds
new errors), and `[tool.basedpyright]` in `pyproject.toml` holds its settings. What it found when the check came in
(2026-11-05, after the fixes of the 2026-11-04 review: 4,418 errors and 1 warning) is frozen in
`tests/type_baseline.json`, which basedpyright writes and reads itself: an error the baseline holds is not reported,
any other fails the run.

```sh
python -m basedpyright                        # the types of evo_agents, against tests/type_baseline.json
python -m tests.structure tighten-types       # rewrite the baseline once you have fixed errors; never adds one
python -m pytest -q -m structure tests/test_structure.py -k type_baseline   # the baseline against origin/main
```

Run it in the environment of a worker's run (`uv sync --frozen` with the extras of `[tool.evo-agents.worker]`): the
types of a missing optional package are unknown, so another set of packages finds other errors. A run takes about four
minutes.

The baseline only shrinks. Against main, it may hold no more errors of a rule in a module than main's does. A module
main's baseline does not name has no entry at all: a new module, and a module split off another, type-checks clean,
so a split fixes the errors of the code it moves. `tighten-types` runs `basedpyright --writebaseline` and puts the old
file back when the code has an error the old one did not hold; do not run `--writebaseline` by hand to make a new
error pass.

## Splitting a module

Split it one seam at a time. A seam is a group of functions, with the state they share, that call each other far
more than they call the rest of the module. The sections a module marks with comments are often seams.

1. Pick the module from the report. For each seam, list who imports its names (`kg_impact` of the evo-kg graph, or
   `grep -rn "from evo_agents.<module> import"`) and which tests patch them.
2. Move the seam into a new module next to the old one. The old module imports back the names its callers use, so
   callers do not change in the same commit.
3. Patch where a name is looked up: once the code that calls `name` lives in the new module, a test patches
   `new_module.name` (unittest.mock, "Where to patch").
4. A private name that the other module now needs gets a public name.
5. Run the tests, `tighten` and `tighten-types`, commit, and go on to the next seam. When no caller imports a name through the old
   module any more, drop its re-export.

## Splitting a function

- Extract each stage (read, decide, act, report) into a helper named for what it does, so the function reads as a
  list of calls.
- Turn a long `if`/`elif` over kinds into a dict from kind to handler.
- Return early for the guard cases instead of nesting.
- A parser builder (`register_*`) splits into one function a subcommand.

## Private names

A name that starts with an underscore belongs to its module. When another module needs it, give it a public name in
its module (drop the underscore and update the callers), or move it into a module both import.
