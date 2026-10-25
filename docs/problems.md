# The problems of a project

A problem says what hurts, before any solution (plan problem-oriented-hub). Each project on the hub keeps its
problems as objects of their own, with the code `P-<number>`, and the rest of the hub points back to them: a goal
answers one problem, a claim of a canvas links the problems it is about, a plan names the problems it solves, and the
Loop's work items carry the problem they go from. A project with no problem works as before: a goal, a claim or a
plan that names none reads, writes and runs on the Loop as it did.

| Part | Where |
| --- | --- |
| the model: codes, fields, statuses | `evo_agents/hub/problems.py` |
| the routes and the writes | `evo_agents/hub/server/problems.py` |
| the tables | `evo_agents/hub/tables_problems.py` (schema 0134) |
| the commands | `evo_agents/hub/cli_problems.py` |
| the MCP tools `problem_list` and `problem_show` | `evo_agents/hub/mcp_tools.py`, `evo_agents/hub/server/mcp.py` |
| a goal's problem | `evo_agents/hub/goals.py`, `evo_agents/hub/server/goals.py` |
| a claim's problems | `evo_agents/hub/server/canvas/links.py`, `content.py` (schema 0135) |
| a plan's problems | `evo_agents/harness/schemas/plan.schema.json`, `evo_agents/hub/server/plan_problems.py` |
| the knowledge graph | `evo_agents/hub/kg_problems.py`, `evo_agents/hub/kg_sources.py` |
| the Loop | `evo_agents/hub/server/loop/rules2.py` (schema 0136, `docs/loop.md`) |
| @builder's proposals of kind `problem` | `evo_agents/core_plugins/builder/proposals.py` |
| the web | `web/src/app/(app)/p/[project]/problems`, `web/src/components/loop` |

## A problem

| Field | |
| --- | --- |
| code | `P-<number>`, given in order within the project and never given again: a problem is closed, never deleted |
| `statement` | what hurts, before any solution; required, at most 2000 characters |
| `who` | who is hurt; required |
| `when` | when it happens; required |
| `workaround` | how they cope today; required |
| `metric` | optional: how much it hurts, a key of kind number of the project's registry of signals (`docs/signals.md`), with `aggregate` (`last`, `mean`, `sum`, `count`, `p50`, `p90`), `window_days` (90 when left out) and `tenant` (the signals of one tenant alone). It has no target: a problem is watched, a goal is reached |
| `rank` | its place among the project's problems, 1 first; a new problem goes after the open ones |
| `owner` | a member with the writer role or above; the writer, for a new problem |
| `status` | `open`, `solved` or `dropped` |
| `close_reason` | why it was solved or dropped, in words (1 to 1000 characters); present exactly when the status is `solved` or `dropped` |
| `note` | what the writer said of this revision, for the history |

`who`, `when` and `workaround` take at most 2000 characters each, and none of the four texts may be blank. The writer
sets the status: the hub never works it out from a claim or a metric. A metric's key must be in the registry and of
kind number when a revision names it anew; a key a revision names already may stay after it is archived.

Every write is the problem's next revision, written whole, and every revision is kept with its writer and time. A
write names the revision it read (`if_revision`); another writer's revision in between makes it a 409, and
`if_revision` 0 creates the problem only. A body equal to the newest revision writes nothing (`changed` false).
Closing writes a revision with the status `solved` or `dropped` and the reason; reopening writes one with the status
`open` and no reason. Each revision adds an audit row `problem.set` that names the problem, the revision and the
fields whose value changed, never a value.

## Who reads and writes

Every member with a grant on the project reads its problems; a member without one gets 404, as for every route of a
project. A writer of the project writes, closes and reopens a problem. The agent of a run never writes one: @builder
proposes one (proposal kind `problem`, target `{"problem": "P-<number>"}`, none for a new problem, whose code the hub
picks), and a writer of the project approves or rejects it in the thread, from the Inbox, on the Problems page (where
the proposals an intake made, which have no thread, wait above the list), with `evo-agents hub intake proposal`, or with
the routes of @builder (`docs/plugins.md`, Builder). Rolling back an approved proposal that made a new problem drops that problem.
Writes of one project's problems wait for each other.

## Links

What a write adds is checked; a link held before stays when its problem is closed later.

- **A goal** answers at most one problem of its project that is not dropped (`problem`, the code). A goal that named
  a problem before that problem was dropped may keep naming it; naming a dropped problem anew is a 422. `evo-agents
  hub goal set --problem P-3` names it and `--no-problem` clears it (`docs/hub.md`, Goals). The Curator's lens
  `product_goals` reads such a goal with `; answers problem P-3`.
- **A claim** of the core plugin canvas links up to 20 problems of its project (`problems`, beside `use_cases` and
  `signal_keys`). A problem a write adds must exist and not be dropped; `solved` is allowed. An undo puts back what the
  rev before held, and an import keeps the problems the project has and leaves the others out with a warning
  (`docs/canvas-plugin.md`, Links).
- **A plan** names the problems it solves in `problems`, a list of codes, each once, after `goal` in the plan's body
  (`plan.schema.json`). A write that names a code the plan did not hold and the project lacks is a 422 that names the
  code, and nothing is written. A PUT that creates a plan or writes a new revision of it naming no problem is stored
  with a warning: naming one is asked, never required. `GET .../plans` and `evo-agents hub plan list --json` list
  each plan's problems, and the copy in git writes them after `goal`; a plan without problems is copied as before.
- **The Loop**'s engine `rules/2` stamps each work item it makes with the problem its goal answers, or with the one
  of the work item it came of (`loop_items.problem_id`, schema 0136). `GET .../loop/items?problem=P-3` keeps the work
  items of one problem, and a chain's stages start from its problem (`docs/loop.md`, rules/2).

`GET .../problems/{ref}` and `evo-agents hub problem show` answer a problem with the goals whose newest revision
answers it, the claims that link it (not archived, in a canvas that is not) and the plans whose body names it, each
of the last two only when the member sees it through the project's hub sink.

## The knowledge graph

A project that has at least one problem gets a source of the hub's own in its graph, `plugin.hub_problems`, read
beside the core plugins' sources; a project with none gets no item, so its graph builds as before. It gives two
Documents with the project's default label:

- Problems (`plugin.hub_problems:doc:problems`): a fragment per problem, keyed by its code, with its statement, who,
  when, workaround, status, rank and metric, so search finds a problem by what it says. The fragment `defines` the
  Problem node `problem:P-3`, which holds the code alone, as a claim's node does.
- Goals (`plugin.hub_problems:doc:goals`): a fragment per goal whose newest revision answers a problem. The fragment
  `defines` the Goal node `goal:G-1`, which `addresses` the problem.

A plan and a claim each `addresses` the problems they name (relation `addresses`, from any kind to Problem), and a
mention of a code such as `P-3` in any source of a project that has problems links to its node. `kg_search "P-3"`
finds the node, and the words of its statement find its fragment. A write of a problem or a goal that changed
something queues a build.

## Commands

```sh
evo-agents hub problem list --project northwind                    # by rank; --status open, solved or dropped
evo-agents hub problem show P-1 --project northwind                # with its goals, claims, plans and revisions
evo-agents hub problem set --statement "Shops run out of stock on Mondays" --who "shop owners" \
  --when "every Monday morning" --workaround "they phone the depot and wait" --project northwind   # P-<next>
evo-agents hub problem set P-1 --metric pantry.stockouts --aggregate sum --window-days 30 --project northwind
evo-agents hub problem set P-1 --rank 2 --note "after the returns problem" --project northwind
evo-agents hub problem close P-1 --status solved --reason "the depot delivers on Sundays" --project northwind
evo-agents hub problem set P-1 --reopen --project northwind
evo-agents hub goal set G-1 --problem P-1 --project northwind      # the goal answers P-1; --no-problem clears it
evo-agents hub canvas edit pantry --claim CL-3 --base-rev 4 --problems "P-1,P-2" --project northwind
```

`set` without a code makes a new problem and needs `--statement`, `--who`, `--when` and `--workaround`; with a code it
changes the options given and keeps the rest, at the revision it reads, unless `--if-revision N` says another. A
metric is `--metric KEY` with `--aggregate`, `--window-days` and `--tenant` optional; `--no-metric` clears it.
`--owner LOGIN` hands the problem to another writer. Each command takes `--project`, or finds `hub.project` in the
`harness.yaml` around the current directory, and `--json` prints what the hub answered (`list` an array).

The MCP tools of `evo-agents hub mcp` read the same: `problem_list` (`project`, `status`) and `problem_show` (`code`,
`revision`, `project`). `canvas__edit` and `canvas__read` of the plugin canvas write and read a claim's `problems`.

## Routes

| Route | Who |
| --- | --- |
| `GET /v1/projects/{project}/problems` (`?status=`): the problems as they are now, by rank, then by code | reader |
| `GET .../problems/{ref}` (`?revision=`): one problem with its goals, claims and plans | reader |
| `GET .../problems/{ref}/revisions`: every revision, newest first, each with the fields it changed | reader |
| `PUT .../problems/{ref}`: the next revision, whole; a code the project has not given yet makes a new problem under the next code | writer |
| `POST .../problems/{ref}/close` (`status` `solved` or `dropped`, `reason`): 409 when the problem is closed already | writer |

The goal's routes take `problem` (`docs/hub.md`, Goals), the canvas's edit takes a claim's `problems`
(`docs/canvas-plugin.md`), and `GET .../loop/items` takes `?problem=` (`docs/loop.md`).

## On the web

Problems sits in the project's menu, before Goals. `/p/{project}/problems` lists the problems in one table, the open
ones first, by status (`?status=`); `/p/{project}/problems/P-3` shows what hurts (who, when, the workaround), its
metric with the figure over its window, a sparkline and the latest signal, the goals that answer it, the claims that
link it by canvas, the plans that name it with their steps done, the Loop's chains that go from it, and its revisions.
A writer adds, edits, closes and reopens a problem there; a reader sees a note. A problem's chip, which names its
statement and opens it, shows on the plans list and a plan's head, a goal's head and a claim's links, and a chain's
rail on the Loop's pages starts from its problem.

## Evidence

A problem holds lines of evidence: something a source says that shows it hurts. A line has the kind of its source
(`hub_chat`, `messenger`, `chat_fpt`, `email`, `file`, `harness`, `other`), the source's URI or path, a place in it (a
message id, a time, a page), the speaker if there is one, an excerpt of at most 300 characters, a signal (`complaint`,
`workaround`, `repeated_request`, `blocker`, `commitment`, `proposed_solution`, `contradiction`,
`decision_without_reason`), a label (`public` or `internal`), who added it, and who confirmed it and when. Lines are
numbered within the problem (E-1, E-2). The hub answers 422 for a `personal` label and for an excerpt over 300
characters: such text stays on the member's machine. The same source, place and excerpt twice is a 409.

```
evo-agents hub problem evidence add P-1 --source-kind hub_chat --source-uri hub://northwind/chats/12 \
  --locator "message 40" --speaker Mira --excerpt "Searches for an old brand name find nothing." \
  --signal complaint --label internal
evo-agents hub problem evidence list P-1 --json
evo-agents hub problem evidence confirm P-1 E-1
```

Routes: `GET|POST /v1/projects/{project}/problems/{ref}/evidence` and `POST .../evidence/{code}/confirm`; the tool
`problem_evidence` lists a problem's lines. A writer adds and confirms, a reader of the project reads, and a member
without a grant gets 404. `problem show` counts the lines and the independent sources they come from (a source is a
kind and an address). A @builder proposal of kind `problem` may carry an `evidence` list; approving it makes the lines
together with the problem, and rolling it back removes them. A proposal of lines alone names `target.problem` and
writes no revision. The graph holds each line as a `doc:evidence` item of the problem (`P-1/E-2`) with an `evidences`
edge to the problem's node, at the project's label. Schema 0137 adds the table `problem_evidence`.

## Intake

An intake (I-1, I-2, ...) is one reading of vague context, such as a chat, a mail thread or a folder, by the session of
a member. `hub intake start` opens it with the kind and label of each source (`KIND:LABEL[:NAME]`, never content). The
session puts what it made of the sources once, as an `evo-intake/1` document (schema
`evo_agents/harness/schemas/intake.schema.json`, validated on put): `candidates` (problems with their evidence lines),
`promises` and `questions`.

```
evo-agents hub intake start --source messenger:internal:"Shop group" --source file:public:notes.md
evo-agents hub intake put I-1 result.json
evo-agents hub intake show I-1 --json
evo-agents hub intake list --status open
```

A put makes a pending @builder proposal of kind `problem` for each candidate: a new problem takes the next free code
(the codes of one put follow each other, so approve them in order), and a candidate with `matches: "P-3"` proposes its
lines as evidence of P-3. A candidate with one independent source that nobody confirmed is refused with 422 and a
reason, and the whole put writes nothing. A candidate is confirmed by `confirmed_by` (a member of the project) or by a
confirmed line of the problem it matches; for a match, the problem's lines count with the candidate's.

Each promise becomes a task of the plugin tasks when the project enabled it: due on the date the session inferred,
assigned to the promiser when they hold a grant on the project and to the member who put the result otherwise. The task
description says who promised, from which source and whether delivery evidence was seen. Without the plugin the
promises stay in the intake and the answer says so (`tasks_skipped`). A put twice is a 409.

Routes: `GET|POST /v1/projects/{project}/intakes`, `GET|PUT .../intakes/{ref}`; the tools `intake_list` and
`intake_show`. Starting and putting need the writer role, reading a grant. Schema 0138 adds the table `intakes`.

## Tables

`problems` holds one row per problem of a project: its number and its newest revision. `problem_revisions` holds every
revision, never changed. Both go with their project. Schema 0134 adds them, `goal_revisions.problem_id` (a key to
`problems` that takes no action of its own, so a project's goals and problems go in one delete) and the @builder
proposal kind `problem`; schema 0135 adds `canvas_claims.problems`, the codes a claim links; schema 0136 adds
`loop_items.problem_id` (set to null when its problem goes) and the work item kind `discover`. Going back past each
drops what it added.
