# The structure of the code

Two checks keep the structure of `evo_agents`: import-linter checks the boundaries between its packages, and the
tests marked `structure` in `tests/test_structure.py` check size and complexity budgets. basedpyright checks its types
against a baseline (section Type check). A plain `pytest` runs none of them (`addopts` deselects the marker); run them
when a change moves code between packages or grows a large module. A failure says what is over, by how much, and how
to fix it. What broke a rule when the rules came in (2026-10-10) was frozen as an exception, and exceptions only
shrink; since 2026-11-05 the baseline of the budgets holds none.

## In CI

No CI run gates these checks. Since 2026-11-11 the CI of a release tag guards only what PyPI gets (the Python suite
on Python 3.12, an import of every module on Python 3.11); the `lint` job of `.github/workflows/ci.yml` runs them only
in a run started by hand with `extras`, in the environment of a worker's run (the extras of
`[tool.evo-agents.worker]`):

```sh
ruff check .
ruff format --check .
lint-imports
git fetch origin +refs/heads/main:refs/remotes/origin/main
EVO_STRUCTURE_BASE=origin/main python -m pytest -q -m structure tests/test_structure.py
python -m basedpyright
```

The Linux test jobs of a tag set `EVO_HUB_TEST_REQUIRE_DSN=1` next to `EVO_HUB_TEST_DSN`: with it set and the DSN
missing, `tests/hub/pg.py` fails the run instead of skipping every hub test (`tests/hub/test_require_dsn.py`). Run the
commands below when a change moves code between packages or grows a large module.

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
| `worker-not-hub-server` | nothing `evo_agents.worker` imports reaches `evo_agents.hub.server`, directly or through another module | none |
| `kg-not-hub-or-worker` | `evo_agents.kg` imports neither `evo_agents.hub` nor `evo_agents.worker` | its CLI: the modules of `kg.cli` (`kg.cli.**`) |
| `hub-server-from-hub-server` | outside `evo_agents.hub.server`, `evo_agents.hub` does not import it | `hub serve` and `hub openapi` (the app), `kg_prune` (audit), `hub worker` (ten job modules) |
| `plugins-standalone` | `evo_agents.plugins`, the code the hub and the worker share to read, trust and place a plugin, imports neither `cli`, `worker`, `hub`, `kg` nor `harness` | none |
| `infra-standalone` | `evo_agents.infra`, the git, files and retry code every package shares, imports no other module of evo_agents (standard library only) | none |
| `packages-acyclic` | `cli`, `worker`, `hub`, `kg`, `harness`, `plugins` and `schema` import each other without cycles | the kg CLI, the MCP proxy's worker home, the CLI contract, the harness loader's use of kg (8) |
| `hub-server-not-cli` | `evo_agents.hub.server` does not import `evo_agents.hub.cli`, directly or through another module: a command imports what it shares with the server from a module outside the CLI | none |
| `hub-runs-layers` | in `evo_agents.hub.server.runs`, `routes` imports `service` and `models`, `service` imports `models`, never the reverse; every module of the package is in one of the three | none |
| `hub-goals-layers` | in `evo_agents.hub.server.goals`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-problems-layers` | in `evo_agents.hub.server.problems`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-opportunities-layers` | in `evo_agents.hub.server.opportunities`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-intake-layers` | in `evo_agents.hub.server.intake`, `routes` imports `service`, `service` imports `opportunity_candidates`, which imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the five | none |
| `hub-signals-layers` | in `evo_agents.hub.server.signals`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-history-layers` | in `evo_agents.hub.server.history`, `routes` and `mcp` (neither importing the other) import `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the five | none |
| `hub-tasks-layers` | in `evo_agents.hub.server.tasks`, `routes`, `agent`, `kg` and `events` (none importing another) import `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the seven | none |
| `hub-srs-layers` | in `evo_agents.hub.server.srs`, `routes`, `agent`, `kg` and `handlers` (none importing another) import `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the seven, and its `__init__` keeps the manifest and its constants | none |
| `hub-decisions-layers` | in `evo_agents.hub.server.decisions`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-kg-decisions-layers` | in `evo_agents.hub.server.kg_decisions`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-memories-layers` | in `evo_agents.hub.server.memories`, `routes` imports `service` (a package: `core`, `agents`, `audience`, `promotion`, `upkeep`, `inputs`), which imports `repository`, which imports `models`, each importing the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-plans-layers` | in `evo_agents.hub.server.plans`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-plan-run-choice-layers` | in `evo_agents.hub.server.plan_run_choice`, `routes` imports `service`, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-plan-systems-layers` | in `evo_agents.hub.server.plan_systems`, `routes` imports `service`, `service` imports `repository`, never the reverse; every module of the package is in one of the three | none |
| `hub-proposal-plans-layers` | in `evo_agents.hub.server.proposal_plans`, `routes` imports `service`, `service` imports `repository`, never the reverse; every module of the package is in one of the three | none |
| `hub-docs-layers` | in `evo_agents.hub.server.docs`, `routes`, `agent` (the tool `docs__read`) and `kg` (the graph's source), none importing another, import `service` and `images`, which import `repository` (its `DocumentStore` a `VersionedStore`), which imports `models` and `page_text`, never the reverse; every module of the package is in one of the four | none |
| `hub-canvas-layers` | in `evo_agents.hub.server.canvas`, `routes`, `agent` (the MCP tools of @strategist), `kg` (the source of the knowledge graph), `value_check` and `schedule` (the jobs), none importing another, import `service`, `service` imports `repository` (`ClaimStore`, a `VersionedStore`), and each imports the layers below it, never the reverse; every module of the package is in one of the four, and the package's `__init__` holds the manifest | none |
| `hub-attachments-layers` | in `evo_agents.hub.server.attachments`, `routes` imports `service` (`uploads`, `content`, `messages`), `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-outputs-layers` | in `evo_agents.hub.server.outputs`, `routes` imports `service` (`store`, `views`, `content`, `serving`, `run_outputs`, `notices`), `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-work-chats-layers` | in `evo_agents.hub.server.work_chats`, which has no routes, `service` imports `repository`, and each imports the layers below it, never the reverse; every module of the package is in one of the three | none |
| `hub-notifications-layers` | in `evo_agents.hub.server.notifications`, `routes` (the member's list and count, the worker's notices, a hub admin's link of a member's channel) imports `service` (`notifier`, whose `NOTIFIER` every module notifies with; `channels`, the Strategy of `Channel` classes; `deliveries`, the job; `notices`, `run_notices`, `reads`, `member_channels`, `admin_channels`), `service` imports `repository` (`outbox`, `listing`, `channels`, `plans`), which imports `models`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-push-layers` | in `evo_agents.hub.server.push`, `routes` imports `service` (`devices`, `tiers`, `senders`, `native`), `service` imports `repository`, `repository` imports `models`, and `models` imports `webpush`, the crypto of Web Push, which imports none of them; never the reverse, and every module of the package is in one of the five | none |
| `hub-webhooks-layers` | in `evo_agents.hub.server.webhooks`, `routes` imports `service` (`hooks`, the admin's CRUD, and `delivery`, the job hub.deliver_webhooks), `service` imports `repository`, which imports `models`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-curator-layers` | in `evo_agents.hub.server.curator`, `routes` (`charter`, the charter, status, nights and pause routes; `threads`, the agent's answer and merge) imports `service` (`charter`, `status`, `shift`, the job hub.fire_schedules, and `threads`), `service` imports `repository`, which imports `models` (`charter`, `settings`, `threads`), and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-playbook-layers` | in `evo_agents.hub.server.playbook`, `routes` imports `service` (the deltas, the views and what the review run's prompt reads), `service` imports `repository`, which imports `models`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-replays-layers` | in `evo_agents.hub.server.replays`, `routes` and `mcp` (the MCP tool curator_replay), which import neither each other, import `service` (the night's replay runs, the claim, the worker's report and the reads), `service` imports `repository`, which imports `models`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-chats-layers` | in `evo_agents.hub.server.chats`, `routes` and `mcp` (the read tool of the hub's /mcp), neither importing the other, import `service`, which checks who may read and write a chat and decides, `service` imports `repository`, which holds every query on the tables (`store` writes a chat's messages, members, reactions and events), and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-evolve-layers` | in `evo_agents.hub.server.evolve`, `routes` (the evolve API and the worker's calls) and `mcp` (the tool `curator_evolve`), neither importing the other, import `service`, which checks the charter, the gates and the claimed run and decides (`runs` queues and ends the evolve runs, `report` takes the worker's report), `service` imports `repository`, which holds every query on the evolve tables, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-digests-layers` | in `evo_agents.hub.server.digests`, `routes` imports `service` (`sessions` writes, lists and prunes the session digests, `today` sends the daily summary push of Today), `service` imports `repository`, which holds every query on the digest and push tables, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-probes-layers` | in `evo_agents.hub.server.probes`, `routes` and `mcp` (the curator_probe_report tool of the hub's /mcp), neither importing the other, import `service` (`probes`, the reads and the store other modules call; `writes`, an admin's and a review run's writes; `runs`, the probe runs; `report`, the weekly report and its notice; `fixes`, what the report asks of a review run), `service` imports `repository`, which holds every query on the tables, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-changes-layers` | in `evo_agents.hub.server.changes`, `routes` (a judge run's inputs and verdict, the list, close and drop of a change) imports `service` (`judge`, `manage`, the job curator.changes in `job`, the night shift's side, the tiers, a change in several repos, `moves`, which writes a change's moves to the ledger, and `policy`, what a run of the Curator gets), `service` imports `repository`, which imports no module of the hub that reaches the service, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-proposals-layers` | in `evo_agents.hub.server.proposals`, `routes` imports `service` (`record`, what a review run or a member's session writes, `reads`, the reads and the owner's answer, and `shapes`, the shapes of a proposal that hold a probe or a system change), `service` imports `repository`, which imports `models`, and each imports the layers below it, never the reverse; every module of the package is in one of the four | none |
| `hub-ops-layers` | in `evo_agents.hub.server.ops`, `routes` (the Orchestrator's ops actions under /v1/worker) imports `service` (`queue`, the pass of the ops queue; `rules`, tier 0; `threads`, the ops threads with their cap, breaker and digest; `actions`, the Orchestrator's actions), `service` imports `repository`, which holds every query on the ops tables, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |
| `hub-triage-layers` | in `evo_agents.hub.server.triage`, `routes` (the triage routes of the worker protocol under /v1/worker) imports `service` (`threads`, the threads of a failed or stale run; `chats`, the triage chats they open in; `facts`, what a thread tells of its run; `actions`, what the agent of a thread does on its own), `service` imports `repository`, which holds every query on the tables, and each imports the layers below it down to `models`, never the reverse; every module of the package is in one of the four | none |

Packages import downward: `cli`, then `worker`, `hub`, `kg`, `harness`, `plugins`, `schema`. import-linter reads imports inside
functions too, so a lazy import does not get around a contract. To fix a broken one, move the shared code down into
a package both may import (a type into `evo_agents.kg` or `evo_agents.schema`, the run protocol into
`evo_agents.hub.runs`), or have the upper package pass it down as an argument. Each contract prints its own advice.

Code outside the CLI does not import a module of the CLI either: what a command shares with the server, the worker or
a client lives in a module of its own that the command imports, such as `evo_agents.hub.plan_writes` (plan writes and
their retry on a revision conflict), `evo_agents.hub.plugin_commands` (the list of the plugins' commands),
`evo_agents.hub.client.signed_in` and `evo_agents.worker.extra`. The CLI modules import those names back for their
other callers.

Inside `evo_agents.hub.server.runs`, the run API, layers go one way too. A route handler passes the request's parts
to a public function of `service`, which checks, queries and moves runs with SQLAlchemy Core on
`evo_agents.hub.tables` and returns the models of `models`. Other modules import `service` and `models`, never
`routes`.

Each contract's `ignore_imports` only shrinks: the test fails a change that adds an entry main does not have. When
you remove the import an entry names, remove the entry too (import-linter fails on an entry that matches nothing).
An entry that names a module git sees renamed from main (`git mv`: a module of main gone and one of nearly the same
content in its place, or a rename a commit of the branch made) counts under the module's new name, so renaming the
module and its entry adds nothing. An entry `P.** -> X` counts as held when main holds `P -> X` for the same
package P, after renaming: it gathers the entries of the modules the package was split into.

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
so a split fixes the errors of the code it moves. A module git sees renamed from main (`git diff --find-renames`
against `origin/main`: a module of main gone and one of nearly the same content in its place, as `git mv` leaves it;
or a rename a commit since `origin/main` made, so a module moved in a commit of its own and rewritten after still
counts) is held to the errors main's baseline holds for its old path, rule by rule. A new file counts only once git tracks
it. `tighten-types` runs `basedpyright --writebaseline` and puts the old
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

## Command classes

New commands are classes of `evo_agents.clikit`, the command kit every package shares (standard library only, it
imports no other package of evo_agents). Every `hub` command is a class in `evo_agents/hub/cli/`, at the path of its
command word (`hub run` is the package `cli/run/`, `hub run-cause` the module `cli/run_cause.py`, `hub agent` the
package `cli/agent/`); `hub release` lives with the Curator's commands in `cli/curator/`. Each module exports one node
per command word, and `tree()` in `evo_agents/hub/cli/__init__.py` builds the whole `hub` tree as one `Group`. The
server commands (`hub serve`, `worker`, `deliver`, `migrate`, `plugin-host`, `storage`) are `ServerCommand`s in
`cli/__init__.py` itself, the one module of the CLI that imports the server's app (`evo_agents.hub.server.app`),
and they need the hub-server extra. `cli/extensions.py` builds one `Group` per plugin of the hub's list, each command a `PluginCommand` over the
plugin's route. The helpers commands share (`signed_in`, `when`, `call_object`) live in `cli/base.py`, the one copy
of each. What a command calls but is not a command stays out of it: `hub kg`'s gateways are in
`evo_agents/hub/kg_routes.py` and `cli/kg/resources.py`, the remote graph session `evo-agents kg` shares with it in
`evo_agents/hub/kg_remote.py`, and `hub openapi` and `hub contract` call `evo_agents/hub/openapi.py` and
`evo_agents/hub/contract.py`. The other parts follow the same layout: `evo_agents/worker/cli/` (`worker step` in
`step.py`, `worker chat` in `chat.py`), `evo_agents/kg/cli/` and `evo_agents/plugins/cli/` (`plugin test` in
`conformance.py`), each with its `tree()` in `__init__.py` and its package base in `base.py`. `tree()` in
`evo_agents/cli.py` puts them under one root `Group`, whose `parser()` is the program's parser, with the `harness`
commands written there; `main` answers `kg hook ...` before it builds any parser. The worker's base
(`evo_agents/worker/cli/base.py`) has `WorkerCommand` for the commands of the machine and `AgentCommand` for those of
a run's agent, which run in an `AgentRunContext`: the run of `EVO_RUN_ID`, its record, the worker's state and token,
checked once against the kinds of run the command serves.

| Class | Pattern | What it does |
| --- | --- | --- |
| `Command` | Command, Template Method | `__call__` checks the options (`check`), builds the context, runs `run`, maps a failure the package knows to its exit status (`failed`) and shows the result. A command writes `run` and the hooks it needs. |
| `ListCommand` | Template Method | `fetch` the items and turn each into a `row` under `columns`. |
| `Group` | Composite | A command word holding commands and groups; it builds its argparse subtree, and `parser()` the program's own parser (the root `evo-agents`). |
| `Result`, `View` (`Text`, `Table`, `Views`, `Verbatim`, `Stderr`) | Strategy, Composite | `run` returns the data `--json` prints and the view a person reads; a command never prints. `Verbatim` writes a rendered document as it is, `Stderr` puts a view (a warning) on stderr in its place, and `Table(..., bare=True)` prints the header when there are no rows. A command that follows a stream (`hub run logs --follow`, `hub run watch`) writes what comes as it comes and returns the rest. |
| `Arg` | Value Object | An argument as a value; options many commands share are constants (`PROJECT`), never mixins. |
| `HubCommand`, `HubContext` (`evo_agents/hub/cli/base.py`) | Dependency injection | The signed-in hub client and the project, built on first use; a `HubError` becomes `error: ...` and the exit status `client.exit_status` gives it. Tests patch `base.signed_in` once. |
| `WorkerCommand`, `AgentCommand`, `AgentRunContext` (`evo_agents/worker/cli/base.py`) | Dependency injection | A worker command's state on this machine, or the run of `EVO_RUN_ID` an agent's command acts for, read on first use and checked against the command's `kinds`; `MissingExtra`, a `HubError` and a `WorkerStateError` become one `error:` line. |
| `ProjectResource` (same module) | Gateway | One collection of a project's API: the one place that quotes the project and the parts of a path. Commands ask their resource and never build `/v1/...` paths. |

Keep inheritance to two levels below `Command` (a kind of command or a package's base, then the command), keep the
logic of the domain out of the command (in the resource, a value object such as `GoalChange`, or the domain module),
and list the children of a `Group` by hand rather than registering classes by import. `configure` sets the defaults
`json_option` sets, so `evo-agents hub contract` reads a class command as it reads a function: a command moved
unchanged leaves `tests/hub/golden/cli-contract.json` as it was, options in the same order.

## Shared infrastructure

`evo_agents.infra` holds the plumbing every package uses alike, on the standard library alone: it imports no other
module of evo_agents (the contract `infra-standalone`), so any package may import it.

| Class | Pattern | What it does |
| --- | --- | --- |
| `Git` (`infra/git.py`) | Template Method | git in one directory (`-C`, or its `cwd` with `chdir=True`), with a timeout and an environment of its own: `run` and `arun` (asyncio) give a `GitResult` (`returncode, stdout, stderr`, unpacked as a tuple); `out`, `read`, `value` and `ok` read one answer. A failure is a typed `GitError`: `GitFailed`, `GitTimeout`, `GitUnavailable`. |
| `Git.fail` | Template Method hook | Turns a `GitError` into the exception the caller raises. A package that had its own git error keeps it and its messages with a subclass (`gitops.RunGit`, `kg_sync.Checkout`, `gitcli.Clone`, `mirror._Checkout`). |
| `write_atomic`, `write_private`, `JsonFile` (`infra/files.py`) | Value Object | A file replaced whole: a temporary file beside it, flushed and renamed over it with `os.replace` (tried again while Windows refuses it), in the mode given (`PRIVATE`, 0600, by default). `JsonFile` reads a document as a value, the default for a file missing, broken or of another shape, and writes it in the format its owner chose. |
| `RetryPolicy`, `Backoff` (`infra/retry.py`) | Strategy | How many tries, which errors try again and the delay before each (`Exponential` with jitter, `Steps`, `Fixed`, or the error's own delay through `asked`, such as a `Retry-After`); `call` and `acall` run a function under it and tell `retrying(error, seconds)` of each wait, `Backoff` counts the failures of a loop that retries on its own (`hubapi.Backoff`, the plugin host's restarts). The worker's calls to the hub take `hubapi.hub_retry(tries)`. |

Write no git helper of your own (`def _git(...)` outside `evo_agents/infra` fails the step's check), no
`_read_json`/`_write_json` pair and no hand-written retry loop: use these, with a subclass or a policy when a module
needs its own messages or delays.

## Hub API gateway

`evo_agents.hub.api` is the one place outside `evo_agents.hub.server` that writes a path of the hub's API
(`/v1/...`): the CLI, the worker and the graph's sync ask the hub through its resources. It imports neither the
server, the CLI nor the worker (the contract `hub-api-standalone`), and the step's check refuses a string that starts
with `/v1/` anywhere else but `evo_agents/core_plugins`.

| Class | Pattern | What it does |
| --- | --- | --- |
| `route` (`api/routes.py`) | | `/v1/<base>/<part>...?<query>`: `base` written as the hub's route is, each part quoted whole, a query value of None left out. |
| `Resource` (`api/resource.py`) | Gateway | One collection, `/v1/<base>/<scope>.../<collection>/<part>...`. `call` goes through any client with `call(method, path, body)` and hands back its answer: a value from the synchronous `hub.client.Hub`, an awaitable from the asynchronous `worker.hubapi.WorkerHub`, so both share the same paths. |
| `ProjectResource`, `FeatureResource`, `KgResource`, `HubRoutes` | Gateway | A collection of a project (`/v1/projects/<p>/...`), of a feature (`/v1/x/<feature>/<p>`), of a project's graph (`/v1/kg/<p>/...`), and routes outside a project under one `base`. A command module declares its collection on one of them. |
| `WorkerRoutes`, `WorkerRun`, `WorkerKg` (`api/worker.py`) | Gateway | The worker protocol's routes and those of one run, which `WorkerHub` and the member's `evo-agents worker` commands both ask. |
| `api/paths.py` | | The routes several modules ask (`PROJECTS`, `WHOAMI`, the blob uploads, `project(name)`). |

A new route of the hub gets its path here: a resource class for a collection a module asks, or a constant of
`paths.py` for a route several modules ask once.


## Hub server base

`evo_agents.hub.server.base` holds what each package of the hub's routes builds on. A package such as `goals`,
`problems` or `opportunities` keeps four layers: `routes` (the FastAPI handlers, which only call the service),
`service` (the rules and the messages), `repository` (the queries) and `models` (the bodies), each importing only the
layers below it.

| Class | Pattern | What it does |
| --- | --- | --- |
| `ProjectScope` (`base/scope.py`) | Dependency | A route's `Depends`: checks the caller has the role (`reader`, `writer` or `admin`) on the project, 403 otherwise, and yields a `Scope` (connection, caller, access, `auditor`) inside one `UnitOfWork`. `check` does the same for a caller that holds its own connection. |
| `UnitOfWork` (`base/unit_of_work.py`) | Unit of Work | One transaction of the engine: it commits when the block ends and rolls back on a refusal. |
| `Auditor` (`base/auditor.py`) | | Writes an audit row for the project and the caller: `record(action, target)`. |
| `DomainError`, `NotFound`, `Conflict`, `Forbidden`, `Unprocessable` (`base/errors.py`) | | A refusal of a domain with its status; the handler of `errors.py` answers it as an `ErrorBody` with the same message. They subclass `HTTPException`, so a caller that catches that still catches them. |
| `StrictModel` (`base/models.py`) | | A body that refuses a field it does not name (`extra="forbid"`). |
| `RevisionedStore` (`base/revisions.py`) | Template Method | A record kept as revisions: `revise` locks the project's records, answers 409 for a stale `if_revision`, takes the number of a new record, writes nothing when the values are those held, and otherwise writes the next revision. A subclass names its tables and codes and reads its records (`GoalStore`, `ProblemStore`, `OpportunityStore`). |
| `VersionedStore` (`base/versions.py`) | Template Method | The variant of `RevisionedStore` for a record that keeps its version on its own row and whose write names the version it was made on (`base_version` of docs and srs, `base_rev` of a canvas claim): `save` locks the record, refuses a stale base with what the domain answers (`stale`, `refusal`; `check_all` for several records of one write), writes nothing when the values are those held, and otherwise writes the next version. |
| `Tool`, `Tools` (`base/tools.py`) | Registry | The MCP tools of a feature (canvas, srs, tasks, docs) as objects of one table: each names its title, description, input schema and handler, and `contributions()` is the manifest's `mcp_tools`. |

A new package of routes takes the same four layers, with a layers contract in `pyproject.toml`.
