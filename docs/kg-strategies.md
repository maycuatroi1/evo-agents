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
