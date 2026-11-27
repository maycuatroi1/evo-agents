"""What a document declares of itself to the project's knowledge graph: its type, its links to other documents, its
level and its audience, in the YAML block its Markdown opens with (``docs/kg.md``, Typed documents).

```yaml
---
type: Campaign          # a node kind the project's ontology.yaml adds
links:                  # relation: the slug of another document, or a list of them
  targets: lumen-audience-makers
  follows: [lumen-brand-voice]
level: customer         # raises the document's label in the graph, never lowers it
audience: chat:12       # the members of chat 12, the brand space; or a list of chats
---
```

A list of chats in ``audience`` narrows the document: a reader must be a member of every chat it names, not of one.

The project's ontology decides what may be declared: ``type`` is a node kind it adds (a hub kind is refused, in any case,
since a document is no commit or plan step), and a relation of ``links`` one it knows, its own or the hub's
(``evo_agents.schema.ontology.known_relations``). A project without an ontology takes no type. A declared type makes the
node ``plugin.docs:<kind in lower case>:<slug>`` of that kind, named by the document's title, which the document
``defines``; each link is an edge from it (from the document itself without a type) to the typed node of the target
document, or to the target's document node when it declares no type. A link to a slug no document has is dropped by
the build as dangling.

The label of the document's item is its stored label joined with what it declares: the higher of the two levels on the
project's ladder (a level the ladder lacks reads as its highest), and an audience of the chats it names. A brand space is
a group chat whose members are the space's members, so ``chat:<id>`` is the audience the hub already gives a chat's
messages, and a reader holds it while they are in that chat (``evo_agents.hub.server.kg_audience``). An audience entry
that is not ``chat:<id>`` cannot be read, and the document is left out of the graph rather than shown to the whole
project (fail closed). What the block declares wrongly in other ways is skipped. The block itself is no text of the
graph: search reads the body below it.

A typed node shows as locked to a member of its audience whose clearance its level is above: kg_node answers that it
exists, its kind and its label, and nothing it holds (``evo_agents.kg.serve_view.SessionView.locked``). A member
outside its audience is told nothing of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

from evo_agents.hub.tables_docs import SLUG
from evo_agents.schema.ontology import HUB_KINDS, entry_name, known_relations
from evo_agents.schema.policy import DEFAULT_LEVELS

FRONT = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.S | re.M)
KIND = re.compile(r"^[A-Z][A-Za-z0-9]*$")
RELATION = re.compile(r"^[a-z][a-z_]*$")
CHAT = re.compile(r"^chat:[1-9][0-9]*$")
TARGET = re.compile(SLUG)
DEFINES = "defines"  # the document to its typed node
DOCUMENT = "document"  # the kind in a document item's id: plugin.docs:document:<slug>


@dataclass(frozen=True)
class Frame:
    """What a project lets its documents declare: its ladder of levels, the node kinds its ontology adds and the
    relations it knows."""

    levels: tuple[str, ...]
    kinds: frozenset[str]
    relations: frozenset[str]

    @classmethod
    def of(cls, levels, ontology: dict | None) -> Frame:
        entries = (ontology or {}).get("node_kinds") or []
        names = {entry_name(e) for e in entries} if isinstance(entries, list) else set()
        hub = {k.lower() for k in HUB_KINDS} | {DOCUMENT}  # the node id lowers the kind: DOCUMENT is Document
        kinds = frozenset(n for n in names if n and KIND.match(n) and n.lower() not in hub)
        relations = frozenset(r for r in known_relations(ontology) if RELATION.match(r))
        return cls(tuple(levels) or tuple(DEFAULT_LEVELS), kinds if ontology is not None else frozenset(), relations)


@dataclass(frozen=True)
class Declared:
    """What one document declares, as its project's frame lets it: ``kind`` None without a type, ``links`` as
    (relation, slug), ``level`` and ``audience`` to join its label with, ``text`` the body below the block, and
    ``closed`` when the document stays out of the graph."""

    text: str
    kind: str | None = None
    links: tuple[tuple[str, str], ...] = ()
    level: str | None = None
    audience: tuple[str, ...] = ()
    closed: bool = False


def split(body: str) -> tuple[dict | None, str]:
    """The mapping of the YAML block ``body`` opens with and the text below it; (None, ``body``) when it opens with
    none, or with one that is not a YAML mapping."""
    found = FRONT.match(body)
    if found is None:
        return None, body
    try:
        data = yaml.safe_load(found.group(1))
    except yaml.YAMLError:
        return None, body
    return (data, body[found.end() :]) if isinstance(data, dict) else (None, body)


def _names(value) -> list:
    return value if isinstance(value, list) else [value]


def _links(value, frame: Frame) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    found = set()
    for rel, targets in value.items():
        if isinstance(rel, str) and rel in frame.relations:
            found.update((rel, t) for t in _names(targets) if isinstance(t, str) and TARGET.match(t))
    return tuple(sorted(found))


def declared(body: str, frame: Frame) -> Declared:
    """What ``body``, a document's Markdown, declares (see the module)."""
    front, text = split(body)
    if front is None:
        return Declared(body)
    kind, level = front.get("type"), front.get("level")
    audience = [] if front.get("audience") is None else _names(front["audience"])
    readable = all(isinstance(a, str) and CHAT.match(a) for a in audience)
    return Declared(
        text,
        kind if isinstance(kind, str) and kind in frame.kinds else None,
        _links(front.get("links"), frame),
        None if level is None else level if isinstance(level, str) and level in frame.levels else frame.levels[-1],
        tuple(sorted(set(audience))) if readable else (),
        closed=not readable,
    )


def node_id(source: str, kind: str, slug: str) -> str:
    """The id of a document's typed node: ``plugin.docs:campaign:<slug>``."""
    return f"{source}:{kind.lower()}:{slug}"


def label(stored: dict, found: Declared, levels: tuple[str, ...]) -> dict:
    """The label of a document's item: its stored label (by names) joined with the level and audience it declares."""
    out = dict(stored)
    held = stored.get("level")
    rank = levels.index(held) if held in levels else len(levels) - 1  # one the ladder lacks reads as its highest
    if found.level is not None and levels.index(found.level) > rank:
        out["level"] = found.level
    if found.audience:
        had = stored.get("audience")
        out["audience"] = sorted(set(found.audience) | set(had if isinstance(had, list) else []))
    return out


def graph(source: str, slug: str, title: str, found: Declared, typed: Mapping[str, str]) -> dict | None:
    """The nodes and edges a document declares (``props.graph``): its typed node, which the document defines, and an
    edge per link to the typed node of the target, else to its document node; ``typed`` maps a slug to its typed
    node's id. None when it declares neither."""
    document = f"{source}:{DOCUMENT}:{slug}"
    nodes, edges = [], []
    start = document
    if found.kind is not None:
        start = node_id(source, found.kind, slug)
        nodes.append({"id": start, "kind": found.kind, "name": title, "props": {"document": slug, "locked": True}})
        edges.append({"src": document, "rel": DEFINES, "dst": start})
    for rel, target in found.links:
        edges.append({"src": start, "rel": rel, "dst": typed.get(target) or f"{source}:{DOCUMENT}:{target}"})
    return {"nodes": nodes, "edges": edges} if nodes or edges else None


__all__ = ["DOCUMENT", "Declared", "Frame", "declared", "graph", "label", "node_id", "split"]
