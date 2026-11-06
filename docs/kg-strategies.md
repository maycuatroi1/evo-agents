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
| `multi-hop-api` | the same prose, on the hub | `multi-hop` with the entities step calling OpenRouter from the build |
| `entity-graph` | prose to look at as a graph of entities and relations | the keys of `documents`, the entities step in its `lightrag` style on Claude Code, the walk |
| `entity-graph-api` | the same prose, on the hub | `entity-graph` with the entities step calling OpenRouter from the build, reasoning off |
| `entity-graph-agent` | prose and code, on the hub | entities read by an agent run that posts an entity map; the build reads the maps and calls no model |
| `entity-graph-all` | every text of a project, on the hub | the entities step of `entity-graph-api` over documents, plans, hub records, chat messages, emails, memories, commits and pull requests, with the maps of `entity-graph-agent` for the code |

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
  edge `mentions` joins the document to each entity. `kg_search` and `kg_context` answer a matched entity with the
  documents that mention it, the entity named as their reason (docs/kg.md, Search).
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
(`POST /v1/kg/P/entities/answers`, a run's token only). Claude Code keeps `CLAUDE_CODE_OAUTH_TOKEN` out of the shell of
its tools, so when the agent runs the command the login is not in its environment: the command asks the run's socket
for it once (`op env`, as `evo-agents worker env --run "$EVO_RUN_ID"` does) and hands that one variable to the `claude`
it starts (`claude_code.run_login`). A run dispatched by hand needs the run's hub token too: a step, plan or author run
with `--as-me` (a chat run has none). The hub keeps the answers to its requests and queues a build,
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

## The multi-hop-api strategy

`multi-hop-api` is `multi-hop` with another model: the build calls an OpenAI-compatible endpoint itself, OpenRouter's
`https://openrouter.ai/api/v1` with `anthropic/claude-haiku-5.5` (`url`, `key_env: OPENROUTER_API_KEY`), so a build on
the hub states the entities of the items it reads at once, with no nightly run of an agent pool. Everything else is `multi-hop`'s: the keys of
`documents`, the ontology it adds, the sink `llm`, the walk, and the ceilings of a build (200 calls, 400000 tokens,
`max_cost` 5), with `price` OpenRouter's of 2026-11-05 (0.10 per million tokens in, 0.50 out) and four calls at once
(`jobs: 4`).

**The key.** On a machine, the build reads `OPENROUTER_API_KEY` from the environment. On the hub, the build opens it
from a member's secret (`evo_agents.hub.server.kg_entity_key`): a live env secret whose variable is
`OPENROUTER_API_KEY`, or a provider account whose base URL is on `openrouter.ai`, either bound to the project for any
worker or owned by the member who created the project, so a project made from an upload uses its creator's key. The
key stays in the build's memory: it is never put in the worker's environment, a log line, a blob, the coverage (which
names `key_from: secret:<id>` alone) or a prompt, and an error of the endpoint is kept with the key cut out. Without a
key the step calls nothing: `coverage["entities"]["stopped"]` says so and the items wait for a later build.

**The sink.** A project made from an upload (`hub kg upload FILE --new-project P`) has the sink `llm`, kind
`llm-backend`, with the clearance of its agent sink (`internal`): an item labelled above it is not sent. Any other
project declares it in its knowledge.yaml as for `multi-hop`.

## The entity-graph strategies

`entity-graph` and `entity-graph-api` read prose as LightRAG does, into a graph a person can look at. Their mapping sets
`style: lightrag`, and the model is asked (`evo_agents.kg.entities.prompt`) for:

- each entity with its name, a type and a description of one sentence. The node kinds of the mapping are a suggestion
  (Actor, Creature, Organization, Place, Event, Concept, Method, Content, Data, Artifact, NaturalObject): a type that
  is none of them is stored as `Other` with the model's word in `props.type`, and LightRAG's Person and Location are
  read as Actor and Place;
- each relation between two of them with keywords, a description and a strength from 1 to 10, stored as the one
  relation `relates_to` with `props.keywords`, `props.description` and `props.strength`.

Each node and edge keeps, as every entity of the step does, the item that stated it (`props.item`), the model
(`props.model`) and its short quote (`props.evidence`), and `props.line`, the line of the item the quote starts on, when
the quote is found in the text sent. A model that gives its evidence as a list of quotes has them joined by ` … `, and
the line is that of the first quote the text holds. The sink and label check, the ceilings, the memo by digest (the style is part of
the digest, so a closed mapping keeps its memo), the Claude Code relay (a request carries `style`) and the url path are
the entities step's. `relates_to` states no `conflicting_claim` in the review queue: it has no predicate, so two objects
of one subject are two links, not two answers.

After all items are read, the model merges and summarizes as LightRAG does at merge time
(`evo_agents/kg/entity_merge.py`; the mapping key `merge`, on by default in the lightrag style only):

- Candidate pairs are `synonym_of` pairs, one name in two kinds, and near names of one kind (the words of one all in the
  other, or alike by `difflib` at 0.85). The model answers 20 pairs a call whether they are one entity and why; each
  answer is memoized by the pair and the model. A pair it calls one is one node, with an edge `merged_from` from that
  node to the document of the name folded in, carrying `reason`. Deprecating that edge with a decision undoes the merge
  at the next build.
- A relation stated more than once (in either direction, after merges) gets `weight`, the sum of its strengths, and
  the union of its `keywords`. Eight descriptions or more, or over 1200 tokens of them, get a `summary` the model writes.
  Descriptions are grouped by label, so a summary mixes one label only and carries it; each item's `mentions` edge
  keeps its own group's summary or `descriptions`.
- These calls use the step's ceiling, sink check and relay. A call past the ceiling waits for a later build; coverage
  counts `merge` (asked, same, pending, refused, undone) and `summaries` (made, pending, refused).

`entity-graph` asks Claude Code as `multi-hop` does; `entity-graph-api` calls OpenRouter as `multi-hop-api` does, with
the same model, price and ceilings, and sends `reasoning: {enabled: false}` (the mapping key `reasoning`, sent to a url
model as the request's reasoning). On 40 MuSiQue paragraphs (seed 7, 2026-11-05, `anthropic/claude-haiku-5.5`), tokens
out per paragraph were 2,232 for `multi-hop-api`, 2,414 for the lightrag prompt with reasoning and 1,274 without it.
`entity-graph-api` is the strategy the Curator starts prose with on the hub.

## The entity-graph-agent strategy

`entity-graph-agent` builds the graph `entity-graph-api` builds, but an agent reads the project instead of a model the
build calls. A run of the project (the Curator's) reads the documents, plans and code itself and posts what it found as
an entity map. The hub checks the map and keeps it, and the step `entity_map` of every build turns the maps kept into
nodes and edges (`evo_agents/kg/entity_map.py`). The build calls no model and needs no key. The node kinds are those of
`entity-graph-api` plus `Component` (a part of a system: a service, module, store or interface) and `Feature`
(something a system does for its users).

**The map, format `evo-entity-map/1`.** One JSON object:

```json
{"format": "evo-entity-map/1", "model": "claude-opus-5-5",
 "entities": [{"kind": "Component", "name": "Entity relay", "type": null, "description": "...",
               "aliases": ["relay"], "conf": 0.9,
               "mentions": [{"node": "<Document, Section, Plan or PlanStep id>", "hash": "<the item's hash read>",
                             "quote": "...", "line": 12}],
               "code": [{"node": "<Directory, File or Symbol id>", "reason": "...", "conf": 0.8}]}],
 "relations": [{"src": "Entity relay", "dst": "Build", "keywords": "...", "description": "...", "strength": 7,
                "mentions": []}]}
```

- `kind` is one of the strategy's node kinds. Any other word makes the entity `Other`, with the word in `props.type`.
- A relation names its two entities by a name or an alias of the same map.
- `hash` is the hash the agent read of the cited item, as `kg_node` shows it. It is optional.
- The hub sets `run`. A map holds at most 5,000 entities and 20,000 relations; post a larger one in parts.

**What the build states.** Every node and edge is `extracted`, so `kg_impact` never walks it:

- a node `entity:KIND:slug` for each entity, with `description`, `aliases`, `type`, `run` and `model`. Names fold as
  the entities step folds them (`entity_links.group`), so two maps, or two parts of one, that name one entity make one
  node;
- `mentions` from each cited Document, Section, Plan or PlanStep to the entity, with `evidence` (the quote) and `line`.
  A citation whose `hash` is not the hash the item has now is `stale: true`, because the item changed after the agent
  read it;
- `relates_to` between two entities, its ends in order, with `keywords`, `description` and `strength`;
- `implemented_by` from the entity to each Directory, File or Symbol it names, with `reason`, `conf`, `run`, `model`
  and `level` (the code's kind). It is a relation of the core schema, as `refers_to` is;
- `refers_to` when the entity's names name one Symbol or File, as the entities step links names.

The label of an entity and of its `mentions` and `relates_to` edges is the join of the labels of the items it cites
(the code's items when it cites none). An `implemented_by` edge also joins the label of the code node it reaches.

**The hub.** `POST /v1/kg/{project}/entity-map` takes the map from the agent of a run (a run's token) with the writer
role; a member's own credential gets 403. The hub reads the cited nodes in its latest build through the run's sink
(`claude-code@anthropic`). An entry that cites a node the build lacks, or a node above that sink's clearance, is
rejected with its reason, and the two cases read the same, so the answer never shows that a hidden node exists. A
mention or a code link is dropped alone. An entity left with nothing cited is dropped, and so is a relation whose end
was dropped. The answer counts what was kept and lists each rejected entry with its reason. What is kept becomes a
blob of kind `kg-entity-map`, and the hub queues a build. Every build reads every map the project holds, so the
worker's cache can still be deleted. Without a graph to check against, the route answers 409.

**Coverage.** `coverage.entity_map` counts the maps and runs, entities (`nodes`, `merged`), relations, `code_links` by
level (Directory, File, Symbol), `name_links`, `mentions`, `stale`, `cited_items` and `uncited_items` (the Documents,
Sections, Plans and PlanSteps of the build that no map cites), and the entries `rejected`, with samples. The hub's
build record keeps the counts alone. `coverage.entities` repeats the node and edge counts with `style: agent`, so the
pipeline view shows the step.

**The runs.** The hub asks for a map itself. Lens `kg_entity_map` of the Curator (`docs/curator.md`) queues a review
run on the project's default agent pool, under the charter's review cost cap, on the first successful build under this
strategy, when a writer calls `evo-agents hub kg entity-map --run --project P`, and when a build's maps have more than
20% of their citations stale or leave more than half of the citable items uncited (at most once a day). One run is in
flight per project. Its prompt holds counts and classes only. The agent reads the outline and what changed through the
run's sink, chooses the level of each code link, may use subagents, and posts one or more partial maps:

```
evo-agents hub kg entity-map --project P [--limit N] [--json]   # the runs, newest first
evo-agents hub kg entity-map --project P --run [--json]         # ask for a run (the writer role)
evo-agents hub kg entity-map --project P --outline [--json]     # repos, directories, code clusters, entities, documents, plans
evo-agents hub kg entity-map --project P --changed [--limit N] [--json]  # new, changed, gone since the last map; uncited
evo-agents hub kg entity-map --project P --post FILE [--json]   # send a map (a run's token)
```

The routes are `POST /v1/kg/{project}/entity-map/run` and `GET .../entity-map/runs`, `.../outline` and
`.../changed`. A run is done when it posted a map, and failed otherwise.

**kg_search and the code of an entity: `entity_code`.** A query key of the KG settings, off by default. Off, kg_search
and kg_context rank on a graph with code links exactly as they ranked before the links existed: an entity the question
matches gives its place to the documents that mention it, and the walk gives `implemented_by` no weight. On, the same
entity then brings up to three code nodes it points to (`implemented_by`, then `refers_to`; those the search ranked
first, the others by the edge's confidence), each naming the entity as its reason, and the walk weighs
`implemented_by` 0.8 (`ppr.ENTITY_CODE_WEIGHT`). The reader's clearance applies to every edge and node, as anywhere
else. The view and `kg_node` show the code links either way.

```
evo-agents kg settings --project P --set entity_code=true --reason "..."
evo-agents hub kg settings --project P --set entity_code=true --reason "..."
evo-agents hub kg measure --project P --settings candidate.json   # {"entity_code": true}: on against off, same build
evo-agents kg bench run ... --settings candidate.json
```

Being a query key, it takes effect at once, and on the hub it can be measured on the newest build before it is kept.

## The entity-graph-all strategy

`entity-graph-all` puts everything a project holds as text into one graph of entities. It runs both steps in each
build:

- `entities`, the mapping of `entity-graph-api` (OpenRouter, `anthropic/claude-haiku-5.5`, sink `llm`, style
  `lightrag`, reasoning off), over the item kinds of every class that holds text: the prose kinds, `plan`, `ticket`,
  `record`, chat `message`, `email`, `memory`, `commit` and `pr`. Code is left to the map's agent, binaries hold no text;
- `entity_map`, as `entity-graph-agent` has it: the maps a Curator run posts tie entities to the directories, files and
  symbols that implement them. The node kinds are `entity-graph-agent`'s, `Component` and `Feature` included.

The mapping's model, kinds, relations, style, reasoning and `max_chars` are those of `entity-graph-api`, so its digest is
the same and a project moving from `entity-graph-api` keeps the answers its memo holds. Its ceilings are higher
(`max_calls` 1,000, `max_tokens` 3,000,000, `max_cost` 5.0, `jobs` 8 per build), and on the hub a build whose step
stopped at its ceiling after at least one answer (`entity_mapping.goes_on`) queues the next build at once, until every
item is read. A build that stopped for another reason, or that read nothing, queues none, so the chain cannot loop.
This applies to every strategy with the `entities` step, not only this one.

## The Curator and prose

A kg_init run whose data profile is mostly prose (`class:prose` a major part) starts with the entities step on: the
prompt says so, and a decision that leaves the project with a strategy whose steps lack `entities` is refused unless one
of its reasons cites `entities_on` and `entities_off`, the figure of the tune questions with the step and without it,
and `entities_off` is the higher (`evo_agents.kg.init_run.parse_decision`). The figures are kept with the decision in
the settings' history.

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
