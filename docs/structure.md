# The structure of the code

Machines keep the structure of `evo_agents`: import-linter checks the boundaries between its packages, and
`tests/test_structure.py` checks size and complexity budgets. CI runs both in the Linux `test` job of tests/worker,
once per Python. A failure says what is over, by how much, and how to fix it. What broke a rule when the rules came in
(2026-10-10) is frozen as an exception, and exceptions only shrink.

## Commands

```sh
lint-imports                                  # package boundaries: [tool.importlinter] in pyproject.toml
python -m pytest -q tests/test_structure.py   # budgets, and the baseline and exceptions against origin/main
python -m tests.structure report              # modules and functions over budget, most over first, then most changed
python -m tests.structure report --json       # the same rows as JSON
python -m tests.structure tighten             # lower the baseline to the code once you have shrunk something
```

The comparison with main reads `origin/main`: run `git fetch origin` first, or it is skipped. CI sets
`EVO_STRUCTURE_BASE=origin/main`, so a missing ref fails there instead.

## Package boundaries

| Contract | Rule | Exceptions today |
| --- | --- | --- |
| `worker-not-hub-server` | nothing `evo_agents.worker` imports reaches `evo_agents.hub.server`, directly or through another module | the parser of `worker plan`, which borrows the hub CLI's JSON option and plan keys (2) |
| `kg-not-hub-or-worker` | `evo_agents.kg` imports neither `evo_agents.hub` nor `evo_agents.worker` | its CLI: `kg.cli`, `kg.cli_graph`, `kg.schedule` |
| `hub-server-from-hub-server` | outside `evo_agents.hub.server`, `evo_agents.hub` does not import it | `hub serve` and `hub openapi` (the app), `kg_prune` (audit), `hub worker` (ten job modules) |
| `packages-acyclic` | `cli`, `worker`, `hub`, `kg`, `harness` and `schema` import each other without cycles | the kg CLI, the MCP proxy's worker home, the CLI contract, the harness loader's use of kg (8) |
| `hub-runs-layers` | in `evo_agents.hub.server.runs`, `routes` imports `service` and `models`, `service` imports `models`, never the reverse; every module of the package is in one of the three | none |

Packages import downward: `cli`, then `worker`, `hub`, `kg`, `harness`, `schema`. import-linter reads imports inside
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

| Budget | Limit | Measured by | Frozen in `tests/structure_baseline.json` |
| --- | --- | --- | --- |
| module | 1,000 lines | lines of the file | 10 modules |
| function | 50 statements, complexity 15 | ruff PLR0915 and C901, `# noqa` ignored | 40 functions |
| private names | none imported from another module | `from module import _name` | 101 imports |

Only `evo_agents` is measured, not the tests. A frozen module or function may not grow past its number. Once it
shrinks, the test asks you to lower the number, and once it fits the budget, to drop the entry: `tighten` does both.
Against main, the baseline may add no entry and raise no number. A function moved unchanged to another module is not
an addition; `tighten` moves its entry. Never raise a number to make the test pass: split the code.

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
5. Run the tests and `tighten`, commit, and go on to the next seam. When no caller imports a name through the old
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
