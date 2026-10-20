# Signals

A signal is something a project measures about its product: a number (the share of searches that found nothing, the
latency of a page) or a piece of feedback in text (what a reader wrote under an article). Services outside the hub send
them in batches with an ingest key; members can push them too, for results they enter by hand. Signals are the hub's,
not a plugin's: the canvas plugin's value check, the Loop and the Curator all read them.

The code is `evo_agents/hub/signals.py` (taking batches, reading, the retention), `evo_agents/hub/server/signals.py`
and `evo_agents/hub/server/signal_keys.py` (the routes), and `evo_agents/hub/cli_signals.py` (the command line). The
tables are `signal_keys`, `signal_ingest_keys` and `signals` (migration 0068).

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

## Retention

The hub keeps a signal for 400 days after it was measured, by default. `EVO_HUB_SIGNAL_DAYS` changes that, from 30 to
3650 days, on the api (which refuses items measured before it) and the worker (whose daily job `hub.prune_signals`, at
05:03, deletes the older signals key by key).
