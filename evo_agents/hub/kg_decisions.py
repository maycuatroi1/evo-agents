"""The decisions writers take on a project's knowledge graph (plan kg-map-and-review, criteria a6 and a7): what they
are, how the next build reads them, and which of them a reader may see.

A decision deprecates a node or an edge, merges two node ids (as ``same_as`` of bindings does: the smallest id stays
and the other becomes its alias) or links two nodes with a relation the ontology knows (an edge with status
``declared``). A writer takes one, active at once; an agent only proposes one (the MCP tool ``kg_propose``), which a
writer accepts or rejects; a writer undoes an active one. Each step names the revision of the project's decisions it
read, and each records who, when, why and the build the graph was at (``kg_decisions``, ``kg_decision_events``,
migration 0142; the routes are ``evo_agents.hub.server.kg_decisions``).

The next build reads the active ones as a source of the hub's own, as it reads the problems (``kg_problems``): SOURCE,
one binding item whose props say ``curation``, with the project's default label, which the binding stage applies
(``evo_agents.kg.pipeline.curation``). A project without an active decision gets no item.

An active decision whose subject the latest build no longer holds is an ORPHAN: it applies to nothing, breaks no
build, and shows in the queue for a writer to undo. A reader sees a decision only when every node and edge it names
that the build holds is visible to them (``subjects``, blocking, called from a WebSession method).
"""

from __future__ import annotations

from collections.abc import Callable

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.kg.policy import TOP_AUDIENCE
from evo_agents.kg.store import Store, edge_id

SOURCE = "plugin.hub_decisions"
ITEM = f"{SOURCE}:binding:decisions"
KINDS = ("deprecate", "merge", "link")
STATES = ("proposed", "active", "rejected", "undone")  # as migration 0142 checks
ORPHAN = "orphan"  # an active decision whose subject the latest build does not hold; never stored
ACTIONS = ("propose", "decide", "accept", "reject", "undo")
MAX_REASON_CHARS = 1000
MAX_REF_CHARS = 1000
# What a step does to a decision: the state it needs and the state it leaves.
TRANSITIONS = {"accept": ("proposed", "active"), "reject": ("proposed", "rejected"), "undo": ("active", "undone")}

VISIBLE, HIDDEN, ABSENT = "visible", "hidden", "absent"


def plugin_actor(plugin_id: str) -> str:
    """How a history or a dismissal names a core plugin that acted as itself (migration 0169): ``plugin:<id>``."""
    return f"plugin:{plugin_id}"


def subject(kind: str, src: str, rel: str | None, dst: str | None) -> tuple[list[str], tuple[str, str, str] | None]:
    """The node ids a decision names, and the edge it names when it deprecates one (a link's edge is its own doing,
    so only its two nodes count)."""
    nodes = [src] if dst is None else [src, dst]
    edge = (src, rel, dst) if kind == "deprecate" and rel is not None and dst is not None else None
    return nodes, edge


def subjects(store: Store, b: int, visible: Callable[[tuple], bool], nodes, edges) -> dict:
    """Whether build b holds each node id of ``nodes`` (directly or as an alias) and each (src, rel, dst) of
    ``edges``, and whether the reader sees it: VISIBLE, HIDDEN or ABSENT, by id and by triple. The store reads every
    audience for the while, so a hidden node is told from a missing one; the reader's audience comes back after."""
    held = store.reader
    store.reader = TOP_AUDIENCE
    try:
        found: dict = {}
        canon: dict[str, str] = {}
        for nid in nodes:
            n = store.node(nid, b)
            if n is None:
                alias = store.alias_of(nid, b)
                n = store.node(alias, b) if alias else None
            canon[nid] = n["id"] if n is not None else nid
            found[nid] = ABSENT if n is None else VISIBLE if visible(n["label"]) else HIDDEN
        for src, rel, dst in edges:
            a, z = canon.get(src, src), canon.get(dst, dst)
            label = store.edge_label(edge_id(a, rel, z), b)
            found[(src, rel, dst)] = ABSENT if label is None else VISIBLE if visible(label) else HIDDEN
        return found
    finally:
        store.reader = held


def seen_state(kind: str, state: str, src: str, rel: str | None, dst: str | None, found: dict) -> str | None:
    """A decision's state as a reader sees it: None when it names something the reader cannot see, ORPHAN for an
    active one whose subject the build does not hold, its stored state otherwise."""
    nodes, edge = subject(kind, src, rel, dst)
    held = [found.get(nid, ABSENT) for nid in nodes] + ([found.get(edge, ABSENT)] if edge else [])
    if HIDDEN in held:
        return None
    return ORPHAN if state == "active" and ABSENT in held else state


def _entry(row) -> dict:
    entry = {"decision": row.number, "kind": row.kind}
    if row.kind == "merge":
        entry["ids"] = [row.src, row.dst]
    elif row.kind == "link" or row.rel is not None:
        entry |= {"from": row.src, "rel": row.rel, "to": row.dst}
    else:
        entry["id"] = row.src
    if row.kind == "deprecate":
        entry["reason"] = row.reason
    return entry


def curation_item(rows: list, label: dict) -> dict:
    """The binding item of the active decisions ``rows`` (number, kind, src, rel, dst, reason), oldest first."""
    text = yaml.safe_dump({"version": 1, "decisions": [_entry(row) for row in rows]}, allow_unicode=True)
    return {
        "id": ITEM,
        "kind": "binding",
        "title": "Decisions",
        "body": {"format": "yaml", "text": text},
        "props": {"path": "decisions.yaml", "curation": True, "decisions": len(rows)},
        "label": label,
    }


async def active(conn: AsyncConnection, project_id: int) -> list:
    """The active decisions of a project, oldest first, each with the reason it was proposed or taken with."""
    d, e = tables.kg_decisions, tables.kg_decision_events
    first = select(e.c.reason).where(e.c.decision_id == d.c.id).order_by(e.c.rev).limit(1).scalar_subquery()
    query = (
        select(d.c.number, d.c.kind, d.c.src, d.c.rel, d.c.dst, first.label("reason"))
        .where(d.c.project_id == project_id, d.c.state == "active")
        .order_by(d.c.number)
    )
    return list((await conn.execute(query)).all())


async def read(conn: AsyncConnection, project_id: int) -> dict:
    """The answer of the source, as a plugin's source answers (``kg_sources.checked``): no item for a project without
    an active decision."""
    rows = await active(conn, project_id)
    if not rows:
        return {"items": [], "identifiers": []}
    label = await conn.scalar(select(tables.projects.c.default_label).where(tables.projects.c.id == project_id))
    return {"items": [curation_item(rows, dict(label or {}))], "identifiers": []}


__all__ = [
    "ABSENT",
    "ACTIONS",
    "HIDDEN",
    "ITEM",
    "KINDS",
    "MAX_REASON_CHARS",
    "MAX_REF_CHARS",
    "ORPHAN",
    "SOURCE",
    "STATES",
    "TRANSITIONS",
    "VISIBLE",
    "active",
    "curation_item",
    "plugin_actor",
    "read",
    "seen_state",
    "subject",
    "subjects",
]
