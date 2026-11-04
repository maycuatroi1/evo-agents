# KG strategies

A strategy says how a knowledge graph is configured for one kind of data. It is a single YAML file, so a person or the
Curator can read it, change it and apply it. `evo-agents kg strategy` lists, shows and applies them on a machine; on the
hub a strategy is the `strategy` key of the project's KG settings.

## Built-in strategies

| Name | For | What it sets |
| --- | --- | --- |
| `code` | source code | nothing: the graph is the one a project without a strategy gets |
| `documents` | long prose | longer passages, vectors written, a longer text per vector |
| `records` | structured records a connector reads | short passages, name weight 8, a `Ticket` identifier, vectors written |
| `conversation` | chat threads, transcripts, agent memories | short passages, vectors weighted 0.8, vectors written |
| `media-metadata` | binary files known by metadata or sidecar text | name weight 8, path weight 3, vectors written |
| `multi-hop` | prose whose answers join facts from several passages | the keys of `documents`, the entities step on Claude Code, the walk |

The built-in files live in `evo_agents/kg/strategies/`.

## The file

```yaml
version: 1
name: crm-records          # lowercase letters, digits and hyphens
description: Customer records read by the crm connector.
suits: [structured]        # classes of the data profile (evo-agents kg profile)
requires:
  connectors: [crm]        # a source of the project must read with one of them
  extras: []               # python packages that must be installed
knowledge:
  identifiers:             # added to knowledge.yaml when the kind is missing
    - kind: Customer
      pattern: '\bCUS-\d{6}\b'
  ontology:                # added to the project's ontology file
    node_kinds: [Customer]
settings:                  # keys of the KG settings (evo-agents kg settings)
  passage_chars: 800
  semantic_weight: 0.8
steps: []                  # optional build steps, such as entities
```

A file with an unknown key, an invalid identifier pattern, a value out of range or a `knowledge` key other than
`identifiers` and `ontology` is refused, and the error names every problem.

A strategy never adds, removes or relabels a source. Labels stay what the project's policy says.

## The entities step

`steps: [entities]` turns on an optional build step that reads prose with a model and states the entities and relations
it says. Nothing runs without it: a project whose strategy does not list the step never calls a model, even when its
settings hold an `entities` mapping (coverage then says so).

The mapping sits in `settings.entities` of the strategy file:

```yaml
steps: [entities]
knowledge:
  ontology:
    node_kinds: [Concept]
settings:
  entities:
    model: my-model            # the name kept as the evidence of what it stated
    command: [my-model-cli]    # a local model: JSON on stdin, JSON on stdout
    node_kinds: [Concept]      # all in the ontology
    relations: [depends_on]
    items: [markdown]          # record kinds read; the prose kinds when absent
    max_cost: 2.0              # ceilings of one build: max_calls, max_tokens, max_cost
    jobs: 1                    # calls at once (default 1)
```

- A model is local (`command`, or a `url` on this machine) or reached through `sink`, a policy sink of kind `llm-backend`
  (with `url`, which needs `pip install 'evo-agents[entities]'`; `key_env` names the variable that holds the key). An
  item whose label the sink does not clear is not sent: `coverage["entities"]["refused"]` lists the items and why.
- What the model states is cut to the node kinds and relations the mapping lists and the ontology holds. Each node
  and edge has the status `extracted`, below `resolved` in rank and left out of impact, and carries the model's name. An
  edge `mentions` joins the document to each entity.
- The answers of all items are read together (`evo_agents/kg/entity_links.py`). Names of one kind that differ only in
  case, accents, spaces, punctuation or number, or that the model gave as `aliases` of each other, are one node; it
  takes the name of the most public item that stated it. An entity whose name or alias names exactly one Symbol or File
  (looked up as `locate` looks names up) gets an edge `refers_to` to it; a name that names more is not linked. When
  the graph has vectors (semantic `index` or `on`), two entities of one kind whose names have a cosine of at least
  `synonym_threshold` (default 0.8) get an edge `synonym_of`. Both edges are `extracted`, so impact never follows them.
  Coverage counts `merged`, `code_links`, `code_ambiguous` and `synonyms`.
- Results are memoized by the item's content digest, the model and the mapping. Each build records calls, tokens and cost
  in `coverage["entities"]` and stops at its ceiling (defaults: 200 calls, 400000 tokens, cost 5). The items left over
  are in `skipped`, the build is not skipped as unchanged, and the next build continues from the memo.
- `jobs` sets how many calls run at once (default 1). Each answer is memoized as it comes, so a build cut short
  by a rate limit goes on from the memo. With more than one call at once, the token or cost ceiling can be passed
  by at most `jobs - 1` calls; `max_calls` counts calls still running.
- Verifying a build reads the memo only and never calls a model.

### Claude Code as the model

`command: [evo-agents, kg, claude-entities]` asks Claude Code (`evo_agents/kg/claude_code.py`), run headless with the
model the mapping names, such as `claude-haiku-5-5`, with no tool, no MCP server and no session kept. Claude Code
sends the text to Anthropic, so the mapping must name an `llm-backend` sink, and each item goes only when that sink
clears its label:

```yaml
settings:
  entities:
    model: claude-haiku-5-5
    command: [evo-agents, kg, claude-entities]
    sink: llm                  # a policy sink of kind llm-backend
    node_kinds: [Concept]
    price: {input: 1.0, output: 5.0}   # per million tokens, when Claude Code reports no cost
```

- The call runs on the login of the machine (`claude login`). `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are taken
  out of its environment, so it never runs on an API key. Coverage counts the tokens in (cache reads and writes
  included) and out, and the cost Claude Code reports, else the tokens priced with `price`. No value of the
  environment is printed: an error that quotes a token shows `[masked]`.
- A login missing or refused, or a usage or rate limit, stops the step at that item (`coverage["entities"]["stopped"]`
  says why). The rest is `skipped` and the next build continues from the memo.
- `EVO_KG_CLAUDE` names the `claude` executable when it is not on `PATH`.

On the hub, the build container has no `claude`. A hub build calls nothing: it defers each item its memo lacks
(`coverage["entities"]["deferred"]`) and keeps those requests, with the text the sink cleared, as a blob the hub holds
(`entity_requests` in the build's coverage, `evo_agents/hub/kg_entity_relay.py`). The prompt of the project's nightly
review run, which runs on the project's default agent pool, then asks it to run `evo-agents hub kg entities --project
P`. That command reads the requests (`GET /v1/kg/P/entities/requests`, the writer role), asks Claude Code for each on
the run's login (the lease of the pool's account, never a personal key), and posts the answers in batches
(`POST /v1/kg/P/entities/answers`, a run's token only). The hub keeps the answers to its requests and queues a build,
which reads them as answers of the step (`answered_elsewhere`) and memoizes them. A login or a limit stops the command
at that item: it posts what it has and exits 3, and the next night goes on.

## The multi-hop strategy

`multi-hop` is for prose where an answer joins facts stated in different passages: specifications, reports, plans
and wiki pages that name the same people, teams, places and things. It holds:

- the keys of `documents` (passages from 2500 characters, passages of 1800, 3000 characters per vector, vectors
  written);
- the entities step (`steps: [entities]`) with a shared ontology of its own, added to the project's ontology file on
  apply: the kinds `Concept`, `Actor`, `Organization`, `Place`, `Event` and `Work`, and the relations `is_a`,
  `has_part`, `located_in`, `belongs_to`, `created_by`, `leads`, `occurred_in`, `causes`, `uses` and `related_to`;
- the model: Claude Code (`command: [evo-agents, kg, claude-entities]`) with `claude-haiku-5-5`, on the login, never
  an API key, through the sink `llm`;
- the folding of an entity's names into one node, the `refers_to` edges to the code it names and, since vectors are
  written, the `synonym_of` edges (`synonym_threshold` 0.8);
- `walk: {}`, the Personalized PageRank walk with its defaults, which ranks kg_search and kg_context.

Use it when the questions people ask need two or more passages ("who leads the team that owns X?"), and the prose
names the same entities in many items. Keep `documents` for prose read one passage at a time, and `code` for source
code: the walk and the entities step do nothing for a graph made of symbols.

**The sink.** Claude Code sends each item's text to Anthropic. Before a build, declare the sink in the project's
knowledge.yaml with the clearance the project accepts:

```yaml
policy:
  sinks:
    - id: llm
      kind: llm-backend
      clearance: {level: internal}
```

Without that sink, or for an item whose label is above its clearance, nothing is sent:
`coverage["entities"]["refused"]` lists the items and why. To use a sink with another id, copy the file to
`<harness>/strategies/multi-hop.yaml` and change `settings.entities.sink`.

**What it costs.**

- One model call per prose item the sink clears, on the first build. Each build stops at its ceiling (200 calls,
  400000 tokens, `max_cost` 5); the rest waits for the next build, which goes on from the memo. An item that has not
  changed costs nothing again. The cost is the one Claude Code reports, else the tokens priced with `price` (1 per
  million in, 5 per million out).
- On the hub, a build defers the items and the nightly review run answers them on the pool's login (see "Claude Code
  as the model" above), so the entities reach the graph a night after the build that asked for them.
- The walk calls no model. kg_context walks twice (seeds, then fill), so its p95 rises with the size of the graph;
  measure it with `kg bench` or `hub kg measure` before keeping the strategy.
- `apply` names `strategy`, `entities`, the passage keys, `semantic_mode` and `ontology` as needing a new build; the
  walk is a query key and takes effect at once.

On the hub, `hub kg settings --strategy multi-hop` sets the settings only. Apply the strategy locally first and push
the knowledge config (`evo-agents hub kg push`), so the hub's ontology holds the kinds and relations and the policy
holds the sink; a kind the ontology lacks is dropped from the mapping (`coverage["entities"]["rejected_config"]`).

## Your own strategy

Put a file of the same format in `<harness>/strategies/NAME.yaml`. It can point at the connectors, the ontology and the
extras it needs. A project strategy wins over a built-in one of the same name. To try a file without keeping it there,
pass its path: `evo-agents kg strategy apply ./crm-records.yaml --project P`.

## Commands

```
evo-agents kg strategy list [--project P] [--json]
evo-agents kg strategy show NAME|FILE [--json]
evo-agents kg strategy apply NAME|FILE --project P [--dry-run] [--json]
evo-agents hub kg settings --project P --strategy NAME|FILE [--reason TEXT]
```

`apply` checks the file and the project first. If the project lacks a connector or an extra the strategy requires,
nothing is changed. Otherwise it writes the settings (kept in the settings history, so `kg settings --undo` puts them
back), adds the identifiers and ontology entries that are missing, and names the keys that need a new build. A file it
changes is copied to `NAME.before-strategy` first. When knowledge.yaml has no `identifiers` key yet the new block is
appended and the file's comments stay; when it has one, the file is written again from its data and its comments are
not kept (the copy keeps them). `--dry-run` prints the same plan and changes nothing.

On the hub, push the knowledge config after applying a strategy locally (`evo-agents hub kg push`), and use
`hub kg settings --strategy` to set the strategy and its settings on the project.
