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
```

- A model is local (`command`, or a `url` on this machine) or reached through `sink`, a policy sink of kind `llm-backend`
  (with `url`, which needs `pip install 'evo-agents[entities]'`; `key_env` names the variable that holds the key). An
  item whose label the sink does not clear is not sent: `coverage["entities"]["refused"]` lists the items and why.
- What the model states is cut to the node kinds and relations the mapping lists and the ontology holds. Each node
  and edge has the status `extracted`, below `resolved` in rank and left out of impact, and carries the model's name. An
  edge `mentions` joins the document to each entity.
- Results are memoized by the item's content digest, the model and the mapping. Each build records calls, tokens and cost
  in `coverage["entities"]` and stops at its ceiling (defaults: 200 calls, 400000 tokens, cost 5). The items left over
  are in `skipped`, the build is not skipped as unchanged, and the next build continues from the memo.
- Verifying a build reads the memo only and never calls a model.

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
