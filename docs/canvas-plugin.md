# The core plugin canvas: a living Lean Canvas on the hub

The plugin **canvas** keeps a Lean Canvas for each product of a project in the hub's database. Each of the nine cells
holds claims, and each claim carries a status, a test card, evidence and every rev it went through. Members edit
claims on the web, on the command line, or by asking @strategist in a group chat. An edit applies at once, can be
undone, and never overwrites a change someone else made in the meantime. A claim reaches `validated` only with real
evidence. The hub's signals (`docs/signals.md`) become evidence through the value check, and a weekly baseline that
members approve makes each change of status take effect and tells the Loop about it.

Its code is `evo_agents/core_plugins/canvas`. evo-ak ships it as a core plugin, so every hub has it (`docs/plugins.md`,
Core plugins), and it does nothing in a project until an admin of the project enables it there.

## Enabling it

```sh
evo-agents hub plugin enable canvas --project northwind
evo-agents hub plugin config canvas --project northwind \
  --set 'baseline={"weekday": "friday", "time": "16:00", "timezone": "Asia/Ho_Chi_Minh"}'
evo-agents hub plugin config canvas --project northwind --effective
```

It asks for no permission. Its config, in the project's layer (`docs/hub.md`, Plugins in a project):

| Field | What it holds | When left out |
| --- | --- | --- |
| `baseline.weekday` | the day of the week the hub proposes each canvas's baseline | `friday` |
| `baseline.time` | the time of day, `HH:MM` on 24 hours | `16:00` |
| `baseline.timezone` | an IANA time zone, such as `Asia/Ho_Chi_Minh` | the time zone of the project's Curator charter, else UTC |

A zone Postgres does not know is logged and the next one in that order is used. Turning the plugin off deletes
nothing: its tables and rows stay for when it is on again, and its routes answer 404 while it is off. A project that
never enabled it has no Lean Canvas entry in its menu.

## The model

**A canvas** is the Lean Canvas of one product: a slug unique in the project, the product's name, a description and a
label. The label is the project's default, or a higher one an admin gives the canvas when making it; every row of the
canvas carries it, and a member who cannot read it gets the 404 of a canvas that does not exist. Any writer makes a
canvas; only an admin archives or unarchives one.

**The nine cells** are `problem`, `customer_segments`, `unique_value_proposition`, `solution`, `channels`,
`revenue_streams`, `cost_structure`, `key_metrics` and `unfair_advantage`.

**A claim** is one statement in a cell. Its fields:

| Field | |
| --- | --- |
| code | `CL-<n>`, unique in the project and never given again, even after the claim is archived |
| `cell`, `position` | where it sits; `position` 0 comes first |
| `text` | the statement |
| `status` | `assumption`, `testing`, `validated`, `invalidated` or `retired` |
| `confidence` | `low`, `medium` or `high` |
| `test_card` | the test of the claim (below), or none |
| `use_cases`, `signal_keys` | the use cases of the plugin srs that realize it and the signal keys that measure it, at most 20 of each (section Links) |
| `archived` | an archived claim leaves the grid and the graph, and keeps its code and revs |

**A test card** follows Strategyzer's Test Card: `hypothesis` (we believe that), `test` (to verify it we will),
`metric` (and measure: a signal key of the project's registry, or `feedback`) and `threshold` (we are right if), with a
`deadline`. Four more fields tell the value check how to compare the signals with the threshold, with no model
involved: `aggregate` (`last`, `mean`, `sum`, `count`, `p50`, `p90`), `comparator` (`>=`, `>`, `<=`, `<`),
`window_days` (90 at most, and 90 when left out), `min_samples` (1 when left out) and `tenant` (only the signals about
that tenant). Every field may be left out until the claim goes to `testing`.

**Evidence** belongs to a claim but sits outside its revs, so adding evidence never bumps a rev or causes a 409. A piece
of evidence has a kind, a direction (`supports`, `contradicts` or `neutral`), a summary, a link, the day it was taken,
a `synthetic` flag and its label. Members add the kinds `interview`, `experiment`, `market`, `doc` and `simulation`,
and @strategist all of them but `experiment`; a simulation is always synthetic. The hub alone writes `signal` and
`feedback`, from the value check.
Evidence is never deleted: a writer withdraws a piece with a reason, and it stays visible as withdrawn. A claim holds at
most 200 pieces.

**A rev** is the whole content of a claim at one moment, numbered from 1, with its provenance: who wrote it (a person,
an agent or the hub), the member it was for, the run with its runtime and model, the chat message that asked for it,
the kind of change (`create`, `edit`, `archive`, `unarchive`, `undo`, `import`, `proposal`, `baseline`) and why. A
diff compares two revs field by field, each field of the test card on its own.

## The rule of evidence

`evo_agents/core_plugins/canvas/rules.py` checks every path that changes a status: an edit on the web, the command line
or the API, an import, an undo, a proposal and its acceptance, @strategist's tools, and the decisions on a baseline.

- A claim goes to `validated` only with at least one piece of real evidence that supports it. Real evidence is not
  synthetic, is of a kind other than `doc` and `simulation`, is not withdrawn, and was added by a person, or added by
  an agent and then confirmed by a writer, or written by the hub from signals that an ingest key or a member pushed.
  Evidence the hub wrote from signals the agent of a run pushed (`rests_on_run`) never counts: an agent cannot measure
  its own claim true.
- An agent never moves a claim to `validated`, whoever asked it to. The route answers 403.
- A claim goes to `testing` only with a test card that holds a hypothesis, a test and a deadline.
- Going to `invalidated` or `retired` needs nothing: dropping a hypothesis is cheap and can be undone.

A refusal is a 422 that says what is missing.

## Editing: the rev you read, at once, and undo

**An edit names the rev each claim was read at** (`base_rev`). The claim's rev goes up only while it is still that
rev, in the transaction that writes the new rev. When a claim changed since, the whole edit is refused with 409 and
nothing is written. The detail holds, for each claim, the rev asked for, the rev it is at, who changed it, both contents
and what differs. On the web, the claim editor then rebases field by field: the fields you changed keep your value,
the others take the newest rev, and you save again. One edit changes at most 100 claims; a change without a claim makes
a new one, which needs a cell and a text.

**An edit applies at once.** No approval is needed to change a claim. The status a claim shows is its current status;
the status in effect for the Loop changes only through a baseline (section Baselines).

**An undo writes a new rev** with the content of the rev before the claim's current rev, so it can itself be undone.
It names the rev it takes back, which must still be the claim's current rev. Undoing a claim's first rev archives the
claim. An undo restores links without checking them again, since they were checked once.

**A run nobody commanded only proposes.** A task run of @strategist (a brainstorm), the value check and, later, the
Loop make proposals instead of revs: a new claim, which starts as an assumption, or a change of a claim at the rev it
was read at. A writer accepts a proposal, which writes the rev as that writer's own (kind `proposal`), or rejects it
with a reason. The rules of status hold when a proposal is made and again when it is accepted. Once its claim gets a
new rev, an open proposal goes stale and can no longer be accepted.

## Links

A claim links the use cases of the plugin srs that realize it and the signal keys that measure it. Only what a write
adds is checked:

- a use case is named by any code or alias an item of the project's SRS ever held, while the project enables srs. The
  item must be a use case, not dropped from its document, in a document the member can read. The claim keeps the code
  the SRS names it by now, so an alias reads as that code;
- a signal key must be in the project's registry and not archived (`docs/signals.md`, The registry of keys).

A link the claim already holds stays when its use case is dropped or its key archived later. An import keeps the
links the project holds and drops the others with a warning.

## Baselines

A claim's status takes effect through an approved baseline: its status in the latest approved snapshot of its canvas
is the status in effect, and a claim no snapshot holds yet is in effect an assumption. The plugin's job `baseline` runs
every 15 minutes and, once the week's time of the config has passed in its time zone, gives each canvas that is not
archived one proposed baseline for that ISO week. A proposed baseline of an earlier week that still waits expires
first, and its undecided claims are asked again.

A baseline asks one item per claim that is not archived, the first of these that holds:

| Item | When | What a reviewer may decide |
| --- | --- | --- |
| `value_check` | the value check has an open proposal to move the claim to a status | accepted, rejected, deferred |
| `status` | members moved the claim away from the status in effect | accepted, rejected, deferred |
| `overdue` | an assumption or a claim in testing whose test card's deadline passed (UTC day) with no real evidence | extended (with a new deadline after today), retired, deferred |
| `new` | a claim no snapshot holds, at `assumption` | nothing: it joins the snapshot as it is |

**Who decides.** A writer of the project, never the agent of a run, and never one of the item's proposers: a member who
changed the claim's status since the status in effect, or accepted the proposal that did, or opened the baseline. An
admin is no exception. What the value check or an overdue card asks, the hub proposed, so any writer may decide it.

**What a decision does**, in the transaction that records it:

- accepted: the status asked takes effect. For a value check item, the hub's proposal is accepted as the reviewer's
  rev, and the rule of `validated` runs again;
- rejected: the status in effect stays, and the hub writes a rev that takes the claim back to it (which can be undone);
- deferred: nothing changes, and the next baseline asks again;
- extended or retired: a rev with the new deadline, or one that moves the claim to `retired`.

A decision other than deferred needs the claim still at the status the item was proposed at, else a 409 asks to defer
it. Once every item has a decision, the baseline is approved: its snapshot holds each claim with its code, rev, status
in effect and the SHA-256 of its content, and it never changes again. A baseline whose items wait for no member (new
claims only, or none) is approved at once with no notice.

**The notice.** A baseline that waits sends the notice `canvas_baseline` to the Inbox of each writer and admin who can
read the canvas and may decide at least one item. Telegram forwards it as it forwards every notice. Its link opens the
baseline's review page, `/p/<project>/x/canvas/canvases/<slug>/baselines/<number>`, where each item is decided with its
evidence. A baseline is a notice, not a decision of the hub, since hub decisions belong to a run.

**Opening one now.** A writer may open a baseline of one claim at once (`POST .../baselines {"claim": "CL-3"}`) for
another writer to approve; it is refused with 409 while a proposed baseline already holds the claim or when the claim
asks nothing.

## Events

The plugin emits two events, in the transaction that made them, for plugins the project granted `canvas:read`
(`docs/plugins.md`, Extension points):

- `canvas.claim` on every rev of a claim, whatever wrote it: the canvas, the claim's id and code, the rev, its cell,
  the kind of change, the status before and after, who wrote it and the run;
- `canvas.baseline` when a baseline is approved: its number and kind, who decided, and each change of status that took
  effect with its evidence. New claims appear as a change from nothing to `assumption`.

A payload carries ids, codes and statuses, never the text of a claim or of evidence, and `v`, the version of its shape.
The hub also writes each event into its table `hub_events`, only ever added to, which the Loop reads by cursor
(`evo_agents.hub.hub_events`).

## @strategist

The plugin contributes the agent type `strategist`: the profile @strategist with the pack `canvas/strategist` (its
instructions and the skills `lean-canvas`, `canvas-editing` and `evidence-judging`). A writer of the project mentions
@strategist in a group chat to read the canvases, edit or undo claims, propose claims, or find signals
(`mentioned_by: writer`). An admin of the project enables the type after the plugin, as for @ba
(`docs/srs-plugin.md`, @ba):

```sh
curl -X PUT "$HUB/v1/projects/northwind/agent-types/canvas/strategist" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"run_as": "alice", "workers": ["alice-mini"], "daily_usd": 5}'
```

The type declares `chat_runs: run_as`, so its chat runs go to a worker of `run_as`, whoever mentioned it. `daily_usd`
caps what its task runs (brainstorms and judging runs together) cost in 24 hours.

**Its tools** are on the hub's /mcp, listed in a session whose project enabled the plugin:

| Tool | What it does | Who may call it |
| --- | --- | --- |
| `canvas__read` | the canvases; a canvas's nine cells with each claim and the open proposals; a claim with its evidence and revs | anyone with a grant |
| `canvas__signals` | the registry of signal keys with the latest value of each, or one key's recent values | anyone with a grant |
| `canvas__edit` | changes claims at the rev read, applied at once as an agent's rev for the member who asked | the agent of a chat run of @strategist, for a writer |
| `canvas__undo` | takes back a claim's rev, or without claims the member's last command | the agent of a chat run of @strategist, for a writer |
| `canvas__propose` | new claims (each an assumption with a test card) or changes at the rev read, for a writer to accept | the agent of a chat or task run of @strategist |
| `canvas__evidence` | adds evidence, which counts toward `validated` only once a writer confirms it | the agent of a chat or task run of @strategist |
| `canvas__judge` | the items of a judging run and a verdict with why on each | the agent of a judging run alone |
| `srs__read`, `srs__search` | the project's SRS documents, when it enables srs | as the plugin srs says |

**A member's command applies at once.** When a writer asks @strategist in chat to change a claim, the change is
written at once as an agent's rev for that writer, with the run, its runtime and model and the chat message. A claim it
makes starts as an assumption. "Undo" in chat takes back the newest message of that member whose revs no undo took
back yet; every claim must still read as the command left it, else the undo is a 409 and nothing is written.

**A brainstorm only proposes.** `POST .../canvases/{slug}/brainstorm` or `evo-agents hub canvas brainstorm` hands
@strategist a task run on a worker of `run_as`, within the type's cap. The run reads the canvas, its signals and the
SRS, and proposes the assumptions the canvas lacks, each with a test card that holds a hypothesis and a test. When no
run can be handed out (the type is off, `run_as` no longer holds writer, or the cap is spent), the route answers 409
with why.

**Simulated users are flagged.** Evidence and proposals that rest on simulated people carry `synthetic`, and a
simulation always does. Synthetic evidence never validates a claim.

**A judging run** is the task run the value check hands @strategist to judge feedback and evidence that is no number
(`docs/signals.md`, Value check). It starts with a fresh context and may call `canvas__judge` alone; every other tool of
the plugin refuses it.

**What @strategist never does**: move a claim to `validated`, confirm evidence, accept or reject a proposal, decide an
item of a baseline, approve a baseline, archive a canvas or change the plugin's config. The routes for those refuse the
token of a run.

## On the web

The canvases are a native page of the plugin, Lean Canvas in the project's menu
(`/p/<project>/x/canvas/canvases`). Under it:

- a canvas (`.../canvases/<slug>`): the grid of nine cells with a card per claim and its status chip, the claims by
  status, and the open proposals to accept or reject;
- a claim (`.../canvases/<slug>/claims/CL-3`): its evidence pane with the form that adds evidence, the judgings of
  @strategist, its history of revs with the diff of each, and undo;
- a baseline (`.../canvases/<slug>/baselines/<number>`): the review, each item with its evidence and the reviewer's
  form.

Readers read everything and change nothing. The project's Signals page (`/p/<project>/signals`) shows the keys of its
registry with their latest value and the series of one key over time (`docs/signals.md`, On the web).

## Import and export: evo-canvas/1

`evo-agents hub canvas export` writes a canvas as `evo-canvas/1` JSON: the canvas, every claim (archived ones included)
with its code, rev and content, and the evidence the member may read. `import` reads it back and keeps the claims'
codes:

- a claim the canvas already holds is changed as an edit is, at the rev the file gives, so a claim changed since makes
  the whole import a 409 and nothing is written;
- a claim the project does not hold is made with the code the file gives, or the project's next code;
- a code another canvas of the project holds refuses the import;
- evidence of kinds `signal` and `feedback`, withdrawn evidence, and links the project does not hold are left out, each
  with a warning; agent evidence nobody confirmed stays unconfirmed.

`--dry-run` does the whole import in a savepoint and rolls it back, so it reports exactly what a real import would do.

## Commands

```sh
evo-agents hub canvas create pantry --project northwind --name "Northwind Pantry"
evo-agents hub canvas list --project northwind
evo-agents hub canvas show pantry --project northwind                       # the grid, claim by claim
evo-agents hub canvas show pantry --project northwind --claim CL-3          # a claim, its revs, evidence and links
evo-agents hub canvas edit pantry --project northwind --cell problem --text "Shops run out of stock on Mondays"
evo-agents hub canvas edit pantry --project northwind --claim CL-3 --base-rev 2 --status testing \
  --test-card '{"hypothesis": "Shops reorder weekly", "test": "Track reorders", "metric": "pantry.reorders", "aggregate": "mean", "comparator": ">=", "threshold": 3, "deadline": "2026-12-01"}'
evo-agents hub canvas edit pantry --project northwind --claim CL-3 --base-rev 3 --use-cases "UC ORD-1" \
  --signal-keys pantry.reorders
evo-agents hub canvas edit pantry --project northwind --file changes.json   # {"changes": [...], "reason": "..."}
evo-agents hub canvas undo pantry CL-3 --project northwind --rev 4
evo-agents hub canvas evidence add pantry CL-3 --project northwind --kind interview --direction supports \
  --summary "Five shop owners reorder every Monday" --on 2026-10-20
evo-agents hub canvas evidence confirm pantry CL-3 12 --project northwind  # agent evidence, by a writer
evo-agents hub canvas evidence withdraw pantry CL-3 12 --project northwind --reason "Wrong shop"
evo-agents hub canvas proposal list pantry --project northwind --status open
evo-agents hub canvas proposal accept pantry 7 --project northwind
evo-agents hub canvas proposal reject pantry 8 --project northwind --reason "Out of scope"
evo-agents hub canvas brainstorm pantry --project northwind --cell channels --focus "small shops"
evo-agents hub canvas export pantry --project northwind --output pantry.json
evo-agents hub canvas import pantry.json --project northwind --dry-run
```

`edit` without `--claim` makes a new claim; `--no-test-card` takes the card away, `--archive` and `--unarchive`
archive a claim or bring it back, and `--reason` is kept with the rev. Baselines have no command: the web and the routes
below decide them.

## Routes

Under `/v1/x/canvas/{project}/`, for a member with a grant on a project that enabled the plugin; reads go through the
sink `X-Evo-Sink` names, else the project's hub sink.

| Route | Who |
| --- | --- |
| `GET` and `POST canvases` (`?archived=true` lists archived ones too) | read; make: writer, a label of its own: admin |
| `GET canvases/{slug}`, `GET .../export`, `GET .../proposals` | reader |
| `GET canvases/{slug}/claims/{ref}`, `.../diff`, `.../judgings` | reader |
| `POST canvases/{slug}/edit`, `POST .../undo` | writer |
| `POST canvases/{slug}/claims/{ref}/evidence`, `.../evidence/{id}/withdraw`, `.../evidence/{id}/confirm` | writer |
| `POST canvases/{slug}/proposals/{id}/accept`, `.../reject` | writer |
| `POST canvases/{slug}/brainstorm`, `POST canvases/import` | writer |
| `GET` and `POST canvases/{slug}/baselines`, `GET .../baselines/{number}`, `POST .../baselines/{number}/items/{ref}/decide` | read: reader; open and decide: writer |
| `POST canvases/{slug}/archive`, `.../unarchive` | admin |

A run's token is refused on every route that decides: confirming evidence, accepting or rejecting a proposal, deciding
an item. A refusal is the hub's error body, with the claims of a conflict or the problems of an import in `detail`.
Every write that changes a canvas or its claims queues a build of the project's knowledge graph.

## The knowledge graph

The plugin's source `canvases` gives each canvas that is not archived as a Document of the graph, with the product's
name and the canvas's label. Each cell that holds a claim is a Section, and each claim a fragment under its cell: its
text, status, confidence, test card and signal keys, so search finds a claim by what it says. Each claim is a Claim node
`claim:CL-3` that holds its code alone, so a mention of `CL-3` in a chat, a plan or a document links to it:

- the claim's fragment `defines` it;
- each use case the claim links `implements` it while the project enables srs, so kg_path goes from a claim to its use
  cases, their tasks and the plan steps of those (`docs/tasks-plugin.md`, The knowledge graph);
- the claim is `measured_by` each signal key it links and the key its test card's metric names, a SignalKey node
  `signalkey:<key>`.

A project ontology that declares the kinds Claim or SignalKey itself fails validation, since the hub declares them.

## Tables

The hub's migrations own the plugin's tables: `canvas_canvases`, `canvas_claims`, `canvas_claim_revs` and
`canvas_evidence` (schema 0072), `canvas_proposals` (0074), `canvas_judgings` with the basis of hub evidence (0075),
`canvas_baselines` and `canvas_baseline_items` (0076), and the links of a claim (0077). Schema 0073 adds the hub's
signal tables and 0076 the hub's `hub_events` (`docs/signals.md`). Import-linter keeps the plugin in its own package
(contract canvas-plugin-own-package): it imports no other core plugin, and reads the SRS through the hub's tables.
