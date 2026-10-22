# The Loop of a project

The Loop (plan loop-engine) is deterministic code of the hub that keeps a project's product moving between its people's
decisions. A person sets goals with a metric and a target; the Loop turns each change of state that matters (a goal's
new revision, a claim a baseline moved to testing, an approved SRS version, a frame task that is ready, an author run
that wrote a plan, a plan run's merge requests) into work items with ancestors and a key, checks its caps before it
hands out a run, hands each one to a run kind the hub already has, cancels what went stale, opens triage and then asks
a person when a work item does not get on. People decide at three gates on the project's decision board.

No tick of the Loop calls a model: a model runs only inside the runs the Loop hands out, under their own caps. The Loop
is off by default. A project whose Loop is off works as before the plan: no work item is made, runs behave as they
did, the Curator runs as it did, and every older command and route is there.

| Part | Where |
| --- | --- |
| the words of contract loop/1 and the engine's interface | `evo_agents/hub/server/loop/model.py` |
| the engine of this release, `rules/1` | `evo_agents/hub/server/loop/rules.py` |
| what a tick reads of a project | `evo_agents/hub/server/loop/view.py` |
| the tick, which writes what the engine returns | `evo_agents/hub/server/loop/tick.py`, `evo_agents/hub/loop_jobs.py` |
| admission and the caps of the runs | `evo_agents/hub/server/loop/admit.py`, `evo_agents/hub/server/loop/caps.py` |
| the hand-out through run kinds | `evo_agents/hub/server/loop/dispatch.py`, `texts.py`, `author_mode.py`, `runs_of.py` |
| triage, the person, the breaker | `evo_agents/hub/server/loop/stall.py`, `evo_agents/hub/server/loop/breaker.py` |
| on and off with the policy | `evo_agents/hub/server/loop/state.py` |
| the decision board, the month's review, the brief | `evo_agents/hub/server/loop/board*.py`, `notices.py` |
| the routes | `evo_agents/hub/server/loop/routes.py`, `items.py`, `chain.py`; goals in `evo_agents/hub/server/goals.py` |
| the tables | `evo_agents/hub/tables_goals.py`, `evo_agents/hub/tables_loop.py` (schemas 0091 and 0093) |
| the commands | `evo_agents/hub/cli_goals.py`, `evo_agents/hub/cli_loop.py` |

## Goal

A goal says what a project is for and how to tell it is reached: a statement, an optional metric (a signal key of kind
number, an aggregate over a window, a comparator and a target), a deadline, the canvas it drives, a rank, an owner and
a status (`active`, `paused`, `closed` with a reason). Its code is `G-<number>`. An admin writes a goal as its next
revision and every revision is kept; the agent of a run only proposes one through @builder (kind `goal`). A goal of
the Curator's charter becomes a goal without a metric the first time the goals are read. A goal has no budget.
`docs/hub.md`, section Goals, has the commands, the routes and the fields.

The Loop works only for an active goal with a metric. A claim serves the first such goal by rank that names its
canvas; when none names it, it serves the project's goal only if the project has exactly one such goal and that goal
names no canvas.

## Work items

A work item (table `loop_items`) is one piece of work the Loop keeps track of. It has:

- a kind and its cycle: `brainstorm` and `use_case` of the business cycle, `build` and `deliver` of the development
  cycle;
- its ancestors: the goal, the claim, the SRS document, use case and version, the frame task, the plan and the run;
  and the work item it came of (`parent_id`), so a chain reads from the goal to the release;
- a key, unique in the project (below);
- a state and, for some states, a reason;
- how often a run was handed out for it (`attempts`) and how often one failed (`failures`);
- who it stands for (`owner`: `@strategist`, `@ba`, or the owner of worker `loop.worker`), the engine that made it
  (`rules/1`) and a `detail` of what it waits on.

Every change of a work item adds a row to its ledger, `loop_item_events`, in the same transaction; the ledger is only
ever added to. Its kinds are `created`, `ready`, `admitted`, `dispatched`, `waiting`, `failed`, `triaged`, `asked`,
`done`, `cancelled` and `noted`.

| State | Reasons |
| --- | --- |
| `queued` | `breaker`, `concurrency`, `rate`, `worker`, `type_refused` (admission held it), or none (ready to hand out) |
| `running` | none: a run was handed out for it |
| `watching` | `stable` (a baseline settles), `auto_tasks` (the plugin srs makes the frame task), `split` (@ba splits it), `merge_queue`, `triage` |
| `waiting_human` | `srs_review`, `srs_finding`, `srs_changes`, `author_question`, `run_decision`, `loop_decision`, `merge_blocked`, `merge_manual` |
| `done` | none |
| `cancelled` | `claim_invalidated`, `claim_retired`, `goal_paused`, `goal_closed`, `loop_off`, `task_closed`, `no_frame_task`, `person`, `dropped`, `manual` |

## The rule table

The engine `rules/1` reads three sources of change: the rows of `hub_events` past the Loop's cursor, the clock, and
what each unfinished work item waits on, read again on each tick. Runs, plan steps and merge requests are not events:
the Loop reads them from their tables when it reconciles a work item, so it needs no event kind beyond
`srs.version.approved` and `tasks.status` (`docs/plugins.md`).

| # | Change | When | What the Loop does | Key |
| --- | --- | --- | --- | --- |
| 1 | a goal's new revision (the clock) | the goal is active, has a metric, and its target is not met (no signal in the window counts as not met), and it has no brainstorm unfinished | a `brainstorm` | `brainstorm:<goal>:r<revision>` |
| 2 | the weekly look at each goal, at `loop.brainstorm.weekday` and `loop.brainstorm.time` | as row 1, and no brainstorm of the goal since, and no proposal of a claim a brainstorm of the goal made still open | a `brainstorm` | `brainstorm:<goal>:<ISO week>` |
| 3 | `canvas.baseline` moves a claim to testing | the claim serves an active goal with a metric | a `use_case`, in `watching` (`stable`) until the baseline has been approved `loop.stable_hours` | `use_case:<canvas>:<claim>:b<baseline>` |
| 4 | `canvas.baseline` moves a claim to invalidated or retired | | nothing new; the result noted in the claim's chain, a comment on each open frame task of the chain, its unfinished work items cancelled | |
| 5 | `canvas.baseline` moves a claim to validated | | nothing new; the result noted in the claim's chain | |
| 6 | `srs.version.approved` of a version a `use_case` drafted, or adding or changing a use case a claim with a chain links | | that `use_case` done; for each use case the version added, a `build` in `watching` (`auto_tasks`) for the frame task the plugin srs makes of it, when the project enables tasks. The Loop makes no task | `build:<external key of the frame task>` |
| 7 | the frame task is ready (the clock) | group todo, the split run ended, in an active sprint when `loop.require_sprint` | the `build` queued | |
| 8 | a frame task of the chain closed | before a run was handed out for it | its `build` cancelled (`task_closed`) | |
| 9 | the `build`'s author run wrote a plan and ended (the clock) | | the `build` done; a `deliver` of that plan | `deliver:<plan id>` |
| 10 | the plan's merge requests in the merge queue (the clock) | each one through (`done`, or `merged` where the repo names no check job), each repo the queue does not merge marked merged in the plan | the `deliver` done; the release noted in the chain | |
| 11 | a merge request of the chain reverted | | a failure of the `deliver` | |
| 12 | `tasks.status`, `canvas.claim`, `signal.posted`, any other version approved | | nothing; the canvas's value check runs on signals by itself | |
| 13 | a test card past its deadline | | nothing; the canvas's baseline asks to extend or retire it | |
| 14 | a goal paused or closed; the Loop turned off | | its unfinished work items cancelled; runs in flight finish and make nothing more | |

The Loop makes work only from state that took effect: a claim through an approved baseline, a use case through an
approved version. An edit of a canvas or a draft of an SRS document makes nothing. A claim moved to validated makes no
build: the result is written into its chain.

### Keys

A key is built from stable ids of the objects and of the moment that made the work (a goal's revision, an ISO week, a
baseline, a frame task's external key, a plan), never from the id of a row of `hub_events`, so two events that tell the
same thing give the same key. A work item is written with `INSERT ... ON CONFLICT (project_id, key) DO NOTHING` in the
transaction that moves the cursor: reading the same events again, or one event told twice, makes nothing more. A key
longer than 500 characters is cut with the start of its SHA-256 (`tick.fit_key`). The key of a cancelled work item
stays taken, so the same change coming again does not make it again; a person who wants it done again answers the
work item's decision with retry.

## The tick

A tick is the job `hub.loop_tick` of one project, with lock and queueing lock `loop:<project id>`: one runs at a time
and at most one waits. `hub.loop_ticks` queues one for each project whose Loop is on every 5 minutes, and
`hub_events.record` queues one in the transaction of each event of such a project. A project whose Loop is off gets
no tick. Each step below runs in a transaction of its own that first takes the project's row of `loop_states`:

1. the events past the cursor, at most `hub_events.MAX_READ` at a time: the engine's `rules` for each, what they
   return written, the cursor moved past them in the same transaction; not while the breaker is tripped, so the cursor
   stands and no event is lost;
2. the engine's `scheduled` (rows 1 and 2), then its `reconcile` of each unfinished work item;
3. the breaker and the stalls (below);
4. admission of each queued work item, oldest first, and the hand-out of the runs that pass.

A tick with nothing new writes no work item. Turning the Loop on (`loop.enabled` true in a new revision of the
policy) puts its cursor at the project's newest event, with no backfill, and queues a tick; a chain from before enters
through the next baseline a writer opens. Turning it off cancels every unfinished work item with reason `loop_off`.

## Admission and caps

Before a run is handed out for a queued work item, admission checks, in order, and the first that fails becomes the
work item's reason; it stays queued and no run is handed out:

1. `breaker`: the Loop's breaker tripped (that the Loop is on is the tick's own check);
2. `concurrency`: the work items of its cycle running reach `loop.business.max_running` or
   `loop.development.max_running` (0 stops that cycle); for a `deliver`, the delivers holding a repo its plan names
   reach `loop.repo_max_running` (a deliver holds a repo while its plan run works there or a merge request of its plan
   on that repo is not merged);
3. `rate`: the runs the Loop handed out in the last 60 minutes reach `loop.dispatches_per_hour`;
4. `worker`: for a `build` or a `deliver`, worker `loop.worker` serves no live daemon of the project, is offline or
   draining, or has no free slot.

A hand-out the agent type refuses (off, its run_as no writer, its daily cap spent) leaves the work item queued with
`type_refused`. A reason is written, with its ledger row, only when it changes.

Each run carries its own caps. An author run and a plan run of the Loop take `max_turns` and `max_seconds` from
`loop.caps.author` or `loop.caps.plan`, `max_usd` empty, and a timeout of that time plus 300 seconds. A task run of
@strategist or @ba is handed to its agent type through `task_dispatch.hand_own_task` like any task run of a core
plugin, so the type's `daily_usd` caps it.

**No budget per goal yet.** The Loop reserves nothing when it admits a work item, holds nothing back for lack of
money, settles no cost into a goal and has no table `loop_charges`; a goal has no monthly budget. The member left this
for later at the checkpoint of step 1 (2026-10-21). A later plan adds it under contract loop/1 as additions only: a
field of Goal, keys of the policy, a reason of `queued` and a table of charges, with no column of a loop/1 table
changed.

## The hand-out

Every run goes through a run kind the hub already has:

| Kind | Run | Stands for |
| --- | --- | --- |
| `brainstorm` | a task run of agent type strategist of the plugin canvas: the goal, its metric and target, the canvas | `@strategist` |
| `use_case` | a task run of agent type ba of the plugin srs: the claim, its test card, the goal and the use cases it links, and a reviewer's words once changes were asked; `srs__propose` takes its version with the goal's owner as author | `@ba` |
| `build` | an author run in Loop mode on worker `loop.worker`: the frame task as its request, its child tasks as the plan's steps, the use case at its version, the claim | the worker's owner |
| `deliver` | a plan run of the plan the build's author run wrote, on worker `loop.worker`, over the repos of its steps not done | the worker's owner |

The owner of `loop.worker` must hold writer on the project. The author and plan runs are dispatched with a machine
credential and the project's default agent profile, linked to the frame task, and audited as `loop.dispatch`. A
worker that cannot take the run, or a plan that cannot run, leaves the work item queued with `worker` and the hub's
why. An author run in Loop mode is ended by the hub once its plan is on the hub and its turn left no decision open;
one that asks a question waits for its owner as any author run does (`author_question`). The merge requests of a plan
run go through the merge queue (`docs/merge-queue.md`) as any plan run's do: the Loop merges nothing itself.

## Waiting for a person, triage and the breaker

A work item waits for a person (`waiting_human`) and counts no failure while its run's agent waits for its owner
(`author_question`, `run_decision`), while the version a `use_case` drafted waits for its review (`srs_review`) or for
the settle chat of an open finding of level decide (`srs_finding`), and while a merge request of its plan is blocked
or paused (`merge_blocked`) or its repo is one the queue does not merge (`merge_manual`). Changes a reviewer asked of
its version queue the `use_case` again with no failure, the request kept in its detail (`srs_changes`) for the next
task run; a version a person withdrew cancels it (`person`).

A failure is a run that failed or was lost for good, stopped on its caps, or ended without what it was for (an author
run with no plan, a plan run with steps not done, a task run with no version in review), or a revert of a merge
request of its plan. A person cancelling the run cancels the work item (`person`).

- The first failure queues the work item again.
- The second puts it in `watching` with reason `triage`: the hub tells the triage thread its last run opened about the
  work item, its failed runs and its chain, or opens a thread in the project's triage chat and asks the project's
  default agent. The third hand-out waits for that thread's chat run to end, at most 60 minutes.
- From the third on, it waits for a person with reason `loop_decision`: a decision of category `loop` on its last run,
  asked of the person that run stands for, with the options retry (queued again, failures counted from 0), drop
  (cancelled, `dropped`) and manual (cancelled, `manual`, its chain left to a person). The hub does the one chosen.
  Telegram shows the decision's buttons as for any decision.

The breaker stops a Loop that runs away: when the work items it made in the last hour (counted from its last reset
when that is later) are more than `loop.max_items_per_hour`, it trips. The tick then reads no event and hands out no
run; runs in flight go on. Each admin of the project gets the notice `loop_paused` in the Inbox and on Telegram, and
only an admin resets it (`evo-agents hub loop resume`, or the button on the Loop page).

## The three gates

People decide the direction of the project at three gates, gathered on its decision board
(`GET /v1/projects/{p}/decision-board`, the web's `/p/{p}/decide`; `docs/hub.md`, The decision board):

1. **Goals**: the month's review of the goals (the notice `goal_review` on the first day of each month in the Loop's
   zone, to the admins and goal owners), and each active goal past its deadline. An admin writes a goal.
2. **Canvas**: each baseline still proposed with an item to decide (a change of status, a status the value check
   proposed, a test card past its deadline), and each open proposal of a claim (a brainstorm's, the value check's, the
   Curator's lens `product`). A writer who did not propose an item of a baseline decides it, any writer once the
   plugin's `self_approval` is on (`docs/canvas-plugin.md`, Baselines).
3. **Policy**: each pending @builder proposal of kind `policy` or `goal`, each repo of the merge queue whose breaker
   tripped, and the Loop's own breaker.

After the gates the board lists what the Loop's chain waits on: its decisions of category `loop`, the decisions and
questions of the runs it handed out, merge requests held for a person, the merge queue's audit samples, and the SRS
versions its use cases drafted that wait for their review, with their settle chats. The review of an SRS version and
the settle chat are no fourth gate: the rules of the plugin srs stay as they are. The Curator's morning brief has a
part of the Loop (the runs it handed out and the work items it finished in the last 24 hours, and those waiting for a
person by reason); a project whose Curator sends no brief gets that part alone at 07:00 in the Loop's zone as the
notice `loop_brief`.

## The policy's keys

The Loop reads the section `loop` of the project's autonomy policy (`evo_agents/hub/autonomy.py`), which an admin
writes as a revision (`evo-agents hub policy`, `docs/merge-queue.md`). A key a revision leaves out reads its default.

| Key | Default | What it sets |
| --- | --- | --- |
| `loop.enabled` | false | the Loop makes work items and hands out their runs |
| `loop.worker` | null | the worker of the Loop's author and plan runs; null: none run |
| `loop.timezone` | null | the zone of the Loop's schedule; null: the charter's, else UTC |
| `loop.business.max_running` | 2 | work items of the business cycle running at once |
| `loop.development.max_running` | 1 | work items of the development cycle running at once |
| `loop.repo_max_running` | 1 | plan runs running plus merge requests not merged, per repo |
| `loop.dispatches_per_hour` | 6 | runs the Loop hands out in 60 minutes |
| `loop.max_items_per_hour` | 30 | work items made in 60 minutes; more trips the breaker |
| `loop.stable_hours` | 0 | hours a baseline stays approved before a work item comes of it |
| `loop.brainstorm.weekday`, `loop.brainstorm.time` | monday, 09:00 | the weekly look at each goal |
| `loop.require_sprint` | false | a frame task is ready only in an active sprint |
| `loop.caps.author.minutes`, `loop.caps.author.turns` | 60, 200 | the caps of an author run the Loop hands out |
| `loop.caps.plan.minutes`, `loop.caps.plan.turns` | 240, 600 | the caps of a plan run the Loop hands out |

## Commands, routes and pages

```sh
evo-agents hub goal list --project demo       # goals by rank
evo-agents hub goal set --statement "..." --metric search.zero_rate --aggregate p50 --comparator "<=" --target 0.05 \
  --window-days 30 --project demo             # a new goal, G-<next>
evo-agents hub loop status --project demo     # on or off, its breaker, cursor, last tick, work items by state
evo-agents hub loop resume --project demo     # an admin resets the breaker that tripped
```

`GET /v1/projects/{p}/loop` is the Loop's state; `GET .../loop/items` its work items (`?state=`, `?kind=`, `?goal=`,
`?open=true`, `?limit=`); `GET .../loop/items/{id}` one work item with its chain, the stages from the goal to the
release, and its ledger; `GET .../loop/goals` each goal's metric now against its target with its work items by state;
`POST .../loop/resume` the admin's reset. Reading needs a grant on the project; each ancestor shows as its own route
shows it, and one the reader may not read is named in `hidden`. The web has the pages Goals (`/p/{p}/goals`), Loop
(`/p/{p}/loop`, by state or by chain) and Decisions (`/p/{p}/decide`); an admin turns the Loop on or off and resets
its breaker on the Loop page, and every other member reads.

## Swapping the reconciler

The engine sits behind `model.Engine`, three functions that read and never write:

```python
class Engine(Protocol):
    name: str

    def rules(self, event: Event, view: View) -> list[Effect]: ...   # what one event makes
    def scheduled(self, view: View) -> list[Effect]: ...             # what the clock makes
    def reconcile(self, item: ItemView, view: View) -> Transition | None: ...  # where a work item moves now
```

An `Effect` is a `WorkSpec` (a work item that should exist, made once by its key), a `Transition` (a work item moves,
counts a failure or spawns the next) or a `Note` (a result written once into a claim's chain). The tick builds the
`View`, calls the engine and writes what it returns; the engine never sees a connection.

| Stable, contract loop/1, additions only | Replaceable |
| --- | --- |
| the tables `goals`, `goal_revisions`, `loop_states`, `loop_items`, `loop_item_events`; the kinds, cycles, states, reasons and ledger kinds of `model` | the rule table: what an event and the clock make |
| the form of each kind's key | the reconcile of each kind of work item |
| the kinds and payloads (`v: 1`) of `hub_events`, the plugins' read permissions | the order of admission and the hand-out's texts |
| the keys `loop.*` of the policy, the fields of Goal | the request of an author run and the text of a task run |
| the routes and commands `hub goal` and `hub loop`, the decision board | |

To swap it, write a class with a `name` and the three functions and pass it to the tick (`tick(engine, project_id,
rules=NewEngine())`), or make it the default in `tick.py` once it is the one. Each work item records the engine that
made it (`loop_items.engine`), so old rows stay readable; a new engine goes on from the cursor of `loop_states` with
the same key forms, so it makes no work item again. A new engine passes the tests the old one passes:
`tests/hub/test_loop_rules.py` hands the engine events and views and reads what it returns (its replay test reads a
chain twice, and with the events renumbered, and gets the same keys), and `tests/hub/test_loop_tick.py` runs ticks on
the hub with Postgres and checks that the same events read again make nothing more.
