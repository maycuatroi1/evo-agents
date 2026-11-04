## Configuring a new graph (the kg_init lens)

Every knowledge graph is configured from its own data profile, not from a habit. A `kg_init` run is a review run: the
hub starts it at the first successful build of a project with a knowledge config, on `evo-agents hub kg init
--project P` (`--all` for a hub admin), and when the profile moved far since the last run. On a machine,
`evo-agents kg init --project P [--questions FILE]` builds the same input from the local graph. Nobody waits for a
person: decide, record, and give the numbers.

### Read the profile

The profile is computed without a model on every build. It says what the data is, so read it before any setting.

```bash
evo-agents hub kg profile --project P --json      # on the hub; evo-agents kg profile --json on a machine
```

Read these in order: the data types and their share of the content lines (code, prose, conversation, records,
binary), the languages of the code and the natural languages of the prose, the sources and their size, the sensitivity
labels, and the build coverage (`warnings`, languages without call edges). A graph that is 5% code and 90% prose is a
documents graph whatever its name says. If the profile is missing (the build came before profiles, or the graph holds
items outside your audience), say so in the decision and take the conservative defaults; do not guess a profile.

### Choose a strategy

```bash
evo-agents kg strategy list --json                # name, what it suits, what it requires, what it sets
evo-agents kg strategy show NAME
```

| Strategy | Use it when | What it sets |
|---|---|---|
| `code` | most content lines are source code | nothing: the default graph |
| `documents` | long prose dominates (manuals, papers, wiki, books) | longer passages, vectors written, a longer text per vector |
| `records` | structured records read by a connector (tickets, customers, rows) | short passages, name weight 8, an identifier such as `Ticket`, vectors written |
| `conversation` | chat threads, transcripts, agent memories | short passages, vectors weighted 0.8, vectors written |
| `media-metadata` | binary files known by metadata or sidecar text | name weight 8, path weight 3, vectors written |

A mixed graph takes the strategy of the data type with the most content lines and keeps the others' needs as single
settings. A strategy whose `requires` the project lacks (a connector, a python extra) is refused by the hub: pick
another, or make one.

### Make a strategy when none fits

Make one when two conditions hold: the profile shows a data type no built-in covers (medical records, music
metadata, customer data from a system of its own), and a measure on the tune split shows the nearest built-in loses to
a configuration you can write down. A strategy is one YAML file (`docs/kg-strategies.md` of evo-agents): a name,
what it `suits` (profile classes), what it `requires`, the `knowledge` it adds (identifiers, ontology, connector), the
`settings`, and optional `steps`. Decide each part from the profile:

- **connector**: the data is not files in a repo. Name the connector the project has; a new connector is a proposal
  for code (`kg_propose`, a tier the charter plans), never an inline script.
- **ontology and identifiers**: the node kinds the data has (Patient, Track, Ticket) and the pattern of its ids.
- **entities step** (`steps: [entities]`): reads prose with a model and states the entities and relations. It costs
  money and sends text to a model, so it is the last resort: use it when prose carries most of the content and the
  tune measure shows words and vectors miss what the entities would link.
- **embedding model**: `semantic_model` for the natural languages of the profile (the default is multilingual);
  `semantic_mode` is `off`, `index` (vectors written, queried by words plus vectors) or `on`.
- **libraries and runtime**: an extra the strategy needs is listed under `requires.extras`; a runtime or dependency
  change is a proposal, not a setting.

Apply a made strategy by file with `evo-agents kg strategy apply FILE --project P --dry-run` first.

### Limits you never cross

- **Label.** An item goes to a model or an API only when the sink of that model or API clears the label of the item.
  The entities step refuses items its sink does not clear and lists them in `coverage["entities"]["refused"]`. A
  graph of `customer` or `restricted` data takes a local model or none. When in doubt, no model.
- **Cost.** Stay under the run's ceiling (the charter's review cost cap) and give each model step its own ceiling
  (`max_calls`, `max_tokens`, `max_cost`). A decision with a step and no ceiling is refused.
- **Holdout.** Measure on the tune split of the question set. Never read the answers of the holdout: not to choose, not
  to check. The hub's measure of the holdout comes without them.

### The rest of the decision

Decide each, with the number that moved it:

| Decision | Settings and commands | Look at |
|---|---|---|
| semantic mode | `semantic_mode`, `semantic_model`, `semantic_weight` | `hub kg semantic --project P`: whether the model is available and how many rows hold a vector |
| passages and weights | `passage_from`, `passage_chars`, `embed_chars`, `bm25_name`, `bm25_path`, `bm25_body` | the profile's passage lengths; tune hit@5 |
| code backend and missing call edges | the build coverage `warnings` and the languages without `calls_across_files` | a language with code and no call edges is a proposal for a backend (`kg_propose`) |
| sources, include and exclude | `knowledge.yaml` sources | `bulk_noise` items and generated files in the profile; a source change is a `kg_source` proposal |

### Measure on the tune split

```bash
evo-agents hub kg measure --project P --split tune --json                       # the project's settings
evo-agents hub kg measure --project P --split tune --settings candidate.json --json   # one candidate on top
evo-agents kg init --project P --questions questions.jsonl --json               # on a machine: tune measures the choices
```

Measure the current settings and two or three candidates the profile suggests, not a grid. A change under two standard
errors of the tune (and under one point of hit@5) is noise: keep the simpler setting. A starter set made from commit
history is small; say so, and do not claim a gain it cannot show.

### Record the decision and undo it

The answer is one JSON object: the strategy, the settings, the sources and code changes, and a reason with the numbers
for each part. Record it, and the hub validates it, writes the strategy and the settings in one change with the reason
and the figures, and queues a rebuild when a build key changed:

```bash
evo-agents hub kg init --project P --decide decision.json          # the run in flight; --run ID for another
evo-agents hub kg init --project P --status --json                 # the runs with their decisions
```

Outside a run, the same writes are `evo-agents hub kg settings --project P --set KEY=VALUE --reason "..." --figures
'{"hit5_tune": 0.41}'` and `evo-agents hub kg settings --project P --strategy NAME --reason "..."`. On a machine:
`evo-agents kg settings --set ...` and `evo-agents kg strategy apply`.

Every change is in the settings history. Undo the newest one that is not undone with `evo-agents hub kg settings
--project P --undo` (`evo-agents kg settings --undo` on a machine). A strategy file's identifiers and ontology are kept
as pending in the run until the hub applies them; sources, KG code and dependencies go through a proposal
(`kg_propose`, the Judge, the auto-merge), which the person can reject.

Old advice stands: a decision that rests only on live examples once lowered hit@5. Measure first, record the numbers
with the decision, and read the figure it should move on the next night.
