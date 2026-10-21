# Signals

A signal is something a project measures about its product: a number (the share of searches that found nothing, the
latency of a page) or a piece of feedback in text (what a reader wrote under an article). Services outside the hub send
them in batches with an ingest key; members can push them too, for results they enter by hand. Signals are the hub's,
not a plugin's: the canvas plugin's value check (section Value check), the Loop and the Curator all read them.

The code is `evo_agents/hub/signals.py` (taking batches, reading, the retention), `evo_agents/hub/server/signals.py`
and `evo_agents/hub/server/signal_keys.py` (the routes), and `evo_agents/hub/cli_signals.py` (the command line). The
tables are `signal_keys`, `signal_ingest_keys` and `signals` (migration 0074), and `hub_events` (migration 0077)
keeps the events of a project the Loop reads.

## The registry of keys

The hub takes a signal only for a key the project declared first. An admin of the project adds a key with its kind,
`number` or `feedback`, and optionally its unit, a description, its source and the level its signals get when a batch
names none:

```sh
evo-agents hub signal keys add kb.search.zero_result_rate --project <project> --kind number --unit ratio --source northwind-kb
evo-agents hub signal keys list --project <project>
evo-agents hub signal keys archive kb.search.zero_result_rate --project <project>
```

A key's name has two to six dotted parts of lowercase letters, digits and `_`, the first starting with a letter
(`^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$`). A name is the project's for good: archiving a key stops it taking new
signals, keeps the signals it has, and leaves the name taken. Any member with a grant lists the keys
(`--archived` shows the archived ones too); adding and archiving need the admin role on the project.

## Ingest keys

An admin of the project makes one ingest key per service:

```sh
evo-agents hub signal ingest create northwind-kb --project <project> --max-level internal
evo-agents hub signal ingest list --project <project>
evo-agents hub signal ingest revoke <id> --project <project>
```

`create` prints the token, `evi_` and 43 characters, once: the hub keeps only its SHA-256 and never shows it again, so
store it in the service's secret store at once. `--max-level` is the highest level a signal written with the key may
carry, a level of the project's ladder; left out, it is the level of the project's default label. Names are unique
among the project's live keys; a revoked key's name is free again. `list` never shows a token (`--revoked` shows the
revoked keys too). Revoking takes effect at the next batch, which gets 401. Creating and revoking write an audit row
naming the project, the key's id and name, never the token.

An ingest key writes signals of its own project through one route and does nothing else. The hub's credential check
refuses it with 403 on every other route, the routes that read signals included, before it asks the database; on the
path of another project it gets 403 too, and a revoked, unknown or malformed key gets 401. Log lines mask anything
shaped like a hub token, `evi_` keys among them.

## Sending a batch

```sh
curl -sS https://hub.example.org/v1/signals/<project> \
  -H "Authorization: Bearer $NORTHWIND_KB_INGEST_KEY" \
  -H "Content-Type: application/json" \
  -d '{"items": [
        {"key": "kb.search.zero_result_rate", "value": 0.12, "measured_at": "2026-10-21T09:00:00+07:00",
         "tenant": "northwind", "external_id": "zr-2026-10-21T09"},
        {"key": "kb.support.feedback", "text": "The article on returns is out of date", "source": "widget"}
      ]}'
```

Each item has:

| Field | |
| --- | --- |
| `key` | a key of the registry, not archived |
| `value` | the number, for a key of kind `number` (finite) |
| `text` | the feedback, for a key of kind `feedback`: at most 4 KiB in UTF-8 |
| `source` | who measured it; the key's source when left out |
| `measured_at` | when it was measured, ISO 8601 with its offset; now when left out |
| `tenant` | who it is about, such as a customer, as free text; the hub has no tenants, this is a dimension of the data |
| `synthetic` | `true` when it comes from simulated people rather than real ones; `false` by default |
| `external_id` | the sender's id of it; the same id sent again with the same ingest key is stored once |
| `level` | its sensitivity, a level of the project's ladder; the key's level, else the project's default |

The hub answers 202 with the batch's id, how many signals it stored (`accepted`), how many repeated an `external_id`
the same ingest key sent before (`duplicates`), and each item it refused (`rejected`), with the item's index in the
batch and why. The other items are stored. An item is refused when its key is not in the registry or is archived,
when it carries text for a number key or a number for a feedback key, when its text is over 4 KiB, when it was
measured more than 10 minutes ahead of the hub's clock or longer ago than the retention, or when its level is not a
level of the project, is above the ingest key's `--max-level`, or is above what the project's hub sink clears.

A signal carries a label like everything on the hub: the project's default label with the item's level (else the
key's, else the default's own), and this project alone. Members read it as that label allows.

A writer of the project pushes with their own credential, through the same route:

```sh
evo-agents hub signal push --project <project> --key kb.search.zero_result_rate --value 0.08 --tenant northwind
evo-agents hub signal push --project <project> --file batch.json
```

`--file` takes `{"items": [...]}` or the list alone (`-` for stdin). For a member, `external_id` is unique per member
in the project. The command prints what the hub stored and each item it refused, and exits 1 when it refused one. A
reader may not push. A project whose knowledge.yaml declares no sink of kind `hub` takes no signal (422), as it takes
no memory.

A batch that stored any signal emits the event `signal.posted` in its transaction: the batch's id, each key with how
many signals it stored and when the first and the last were measured, and the ingest key's name or the member's login
(and the run, for the agent of one); never a value or a text. Plugins the project granted `signals:read` get it
(`docs/plugins.md`, Extension points), and the hub keeps it in `hub_events` for the Loop.

## Limits

So that a service gone wrong cannot fill the database:

- a batch has at most 500 items and at most 1 MiB of body; more items is a 422 and a larger body a 413, read no
  further;
- an ingest key sends at most 60 batches a minute, counted in a fixed window of a minute, refused batches included;
  past that it gets 429 with `Retry-After` until the window ends. Send fewer, larger batches;
- a project takes at most 50,000 signals in any 24 hours, from all its ingest keys and members together; a batch that
  would go past that is refused whole with 429 and `Retry-After` until the oldest signal of those 24 hours leaves the
  window.

## Reading

Any member with a grant on the project reads its signals, newest first:

```sh
evo-agents hub signal list --project <project> --key kb.search.zero_result_rate --since 2026-10-01T00:00:00+00:00
```

`GET /v1/projects/{project}/signals` takes `key` (one key; all when left out), `since` (measured at or after), `until`
(measured before), `tenant` and `limit` (100 by default, 1000 at most). Each signal says who pushed it: `via` is
`ingest` with the ingest key's name, `member` with the member's login, or `run` for the agent of a run pushing for its
owner. A signal is read under its label through the sink `X-Evo-Sink` names, else the project's hub sink, as plans
are: a member whose grant does not reach a signal's level does not see it.

## On the web

A project's Signals page (`/p/<project>/signals`, Signals in the project's menu after Insights) lists the keys of its
registry, each with its kind, unit, source, description and the latest value or text a member may read. Choosing a key
shows its signals over 7, 30 or 90 days or a year, of one tenant when one is chosen: a table, newest first, and for a
number key a line chart too. The page reads `GET /v1/projects/{project}/signals` and adds no route. The evidence the
value check writes links to that page with the key and the tenant (`/p/<project>/signals?key=...&tenant=...`). The
page only shows values and their series over time; it is no dashboard of analytics.

## Signal keys in the knowledge graph

A claim of the core plugin canvas that links a signal key, or whose test card's metric names one, is `measured_by` a
SignalKey node `signalkey:<key>` in the project's knowledge graph (`docs/canvas-plugin.md`, The knowledge graph). The
node holds the key's name alone; no value of a signal goes into the graph.

## Value check

The core plugin canvas compares its claims' test cards with the signals, as evidence and proposals; it never changes a
claim's status itself. The code is `evo_agents/core_plugins/canvas/value_check.py` (the job), `checks.py` (the
comparison, the evidence and the proposals) and `judging.py` (the judging runs of @strategist).

**When it runs.** A batch that stored any signal queues the job `hub.value_check` for its project, in the batch's
transaction, when a plugin the project enables has a job named `value_check` (canvas's). The job runs those jobs for
that project alone; one runs at a time per project, and the batches that come while one waits join it. The plugin's
job `value_check` also runs hourly at minute 11 for every project that enables canvas, which catches what no batch
brings: a deadline that passed, items that waited for a judging run.

**Numbers.** A claim not archived and not retired is checked when its test card names a key of kind `number` in
`metric`, with `aggregate`, `comparator` and `threshold`. The key's signals measured in the last `window_days` (90 when
the card names none), of the card's `tenant` when it names one, are summed up by `aggregate`:

| `aggregate` | |
| --- | --- |
| `last` | the value measured last |
| `mean`, `sum`, `count` | over the window |
| `p50`, `p90` | the nearest rank (Postgres's `percentile_disc`): a value the window holds |

and compared with `threshold` by `comparator` (`>=`, `>`, `<=`, `<`). The result is `met`, `not met`, or
insufficient while the window holds fewer than `min_samples` signals (1 when the card names none). No model is
called: the same signals give the same result.

**Feedback.** Text is judged by @strategist, the canvas plugin's agent type, in a judging run of its own: a task run of
the type, which the hub hands out as the plugin srs hands @ba a review, under the type's `run_as` and within its
`daily_usd` (brainstorms count against the same cap). A judging run gets, for each claim whose test card holds a
hypothesis, the feedback of the key its test card names when that key is of kind `feedback` (in the card's window and
tenant), and the claim's interviews, experiments and market figures with the direction `neutral` that a person added
or a writer confirmed; at most 50 items and 40,000 characters, those the type's `run_as` may read, oldest first. One
judging run of a canvas goes at a time; when none can be handed out (the type is off, or its cap is spent), the items
wait for the next pass, and an item two runs ended without judging is left out.

The run starts with a fresh context and has one tool, `canvas__judge`, which shows each claim's hypothesis and test
card and the items, and records a verdict on each: `supports`, `contradicts` or `irrelevant`, with why. Every other
tool of the plugin refuses a judging run, and `canvas__judge` refuses every other run. The criteria are the skill
`evidence-judging` of the pack `canvas/strategist`: `supports` or `contradicts` only for an item that states something
a real user did, said or experienced, bearing directly on the hypothesis; when unsure, `irrelevant`. A verdict on a
piece of evidence that is not `irrelevant` becomes evidence of kind `feedback` the hub writes. A test card whose
`metric` is a key of kind feedback, or `feedback`, compares how many judged items support the claim (the value; the
samples are those judged to support or contradict it) with its threshold, as for numbers. A member reads every item
and its verdict with `GET /v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/judgings`.

**What it writes.** `met` and `not met` become a piece of evidence the hub writes, of kind `signal` for a number and
`feedback` for feedback, that `supports` or `contradicts` the claim. Its `basis` holds the value, the samples, the
window, the comparison, the result and the ids of the first and last signal or item; its link opens the key's signals
on the web (`/p/<project>/signals?key=...`). It is synthetic when any signal or item it sums up is, and carries the
claim's label joined with theirs. A claim gets at most one such piece a day, and only when the result changed since the
last one; insufficient writes nothing.

**Signals a run pushed.** Evidence the hub writes from signals the agent of a run pushed (`via` = `run`) carries
`rests_on_run`, and never lets a claim go to `validated`: only signals an ingest key or a member pushed count. The
agent of a run cannot measure its own claim true.

**What it proposes.** On the latest result of a claim: `met` while the claim is an assumption or testing proposes
`validated`; `not met` once the test card's `deadline` passed proposes `invalidated`, unless the claim is invalidated
or retired already. Each is a proposal of the hub naming its evidence (`evidence_id`), made once per piece of evidence,
for a member to accept or reject (later on the weekly baseline); a newer result makes the hub's open proposals of the
claim stale. The rules of status hold as on every path, so `validated` is never proposed without real evidence that
supports the claim.

## Retention

The hub keeps a signal for 400 days after it was measured, by default. `EVO_HUB_SIGNAL_DAYS` changes that, from 30 to
3650 days, on the api (which refuses items measured before it) and the worker (whose daily job `hub.prune_signals`, at
05:03, deletes the older signals key by key).
