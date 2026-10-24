"""Curation: the decisions writers of a project took on the hub (``evo_agents.hub.kg_decisions``), applied at the
binding stage of the next build.

The hub gives them as one binding item whose props say ``curation: true`` and whose YAML body lists the active
decisions, oldest first:

    version: 1
    decisions:
      - {decision: 3, kind: merge, ids: [billing:src/a.py, billing:src/b.py]}
      - {decision: 4, kind: link, from: usecase:KB-01, rel: implements, to: billing:src/a.py}
      - {decision: 5, kind: deprecate, id: billing:src/old.py, reason: replaced by a.py}
      - {decision: 6, kind: deprecate, from: a, rel: calls, to: b, reason: the call was removed}

A merge is a ``same_as`` of bindings: the smallest id stays and the others become its aliases. A link is an edge with
status ``declared`` and the prop ``decision``. A deprecation marks the node or edge it names with the prop
``deprecated`` (its reason and decision) once the graph is assembled (``apply``); the tools leave such parts out
unless asked (``evo_agents.kg.serve``). Unlike a binding file, a decision whose ids the graph no longer holds is an
orphan, never an error: it is skipped and the hub shows it in its queue, so no decision can break a build.
"""

from __future__ import annotations

import yaml

from evo_agents.kg.pipeline.binding import binding_item, resolve
from evo_agents.kg.pipeline.stages import edge, item_unit
from evo_agents.kg.store import Graph, edge_id

CURATION_VERSION = "1"
CURATION_FACTS = ("same", "deprecate")  # the facts assembly keeps until entity resolution


def is_curation(record: dict) -> bool:
    return bool((record.get("props") or {}).get("curation"))


def bind(record: dict, text: str | None, index: dict) -> list[dict]:
    """The binding stage of one binding item: a curation item's decisions, or a binding file's bindings."""
    if is_curation(record):
        return curation_item(record, text, index)
    return binding_item(record, text, index)


def _existing(value, index: dict) -> str | None:
    """The id ``value`` names, or None. An identifier's id (``requirement:KB-01``) stands as it is, but a decision never
    creates its node: the edges of a link need both their nodes, and a merge only joins nodes the graph holds."""
    if not isinstance(value, str) or not value.strip():
        return None
    code_kinds = {i["kind"].lower(): i["kind"] for i in index.get("identifiers", [])}
    found, _create = resolve(value, index, code_kinds)
    return found


def _decision_facts(entry: dict, unit: str, index: dict) -> list[dict]:
    number, kind = entry.get("decision"), entry.get("kind")
    where = [[unit, f"decision {number}"]]
    if kind == "merge":
        ids = [_existing(value, index) for value in entry.get("ids") or []]
        if len(ids) < 2 or not all(ids):
            return []
        return [{"t": "same", "a": ids[0], "b": other, "units": [unit], "where": where} for other in ids[1:]]
    if kind == "link":
        src, dst, rel = _existing(entry.get("from"), index), _existing(entry.get("to"), index), entry.get("rel")
        if not (src and dst and isinstance(rel, str) and rel):
            return []
        props = {"decision": number}
        return [edge(src, rel, dst, [unit], props=props, status="declared", needs=[src, dst], where=where)]
    if kind == "deprecate":
        mark = {"reason": str(entry.get("reason") or ""), "decision": number}
        keys = ("id",) if entry.get("id") else ("from", "rel", "to")
        return [{"t": "deprecate", **{k: str(entry.get(k) or "") for k in keys}, **mark}]
    return []


def curation_item(record: dict, text: str | None, index: dict) -> list[dict]:
    """The facts of a curation item's decisions; an entry that is not a decision, or names what the graph does not
    hold, gives none."""
    unit = item_unit(record["id"])
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return []
    entries = data.get("decisions") if isinstance(data, dict) else None
    facts: list[dict] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            facts.extend(_decision_facts(entry, unit, index))
    return facts


def split(facts: list[dict], deprecations: list[dict]) -> list[tuple[str, str]]:
    """The identities among ``facts`` (``same``), as pairs; their deprecations go to ``deprecations``."""
    pairs = []
    for f in facts:
        if f["t"] == "same":
            pairs.append((f["a"], f["b"]))
        else:
            deprecations.append(f)
    return pairs


def apply(graph: Graph) -> dict:
    """Mark each node and edge a deprecation of ``graph`` names, through the aliases of merged nodes; how many were
    marked and how many named nothing the graph holds (orphans)."""
    alias = {a: nid for nid, n in graph.nodes.items() for a in n.get("aliases") or []}

    def canon(x: str) -> str:
        return alias.get(x, x)

    marked = orphans = 0
    for f in graph.deprecations:
        if "id" in f:
            target = graph.nodes.get(canon(f["id"]))
        else:
            target = graph.edges.get(edge_id(canon(f["from"]), f["rel"], canon(f["to"])))
        if target is None:
            orphans += 1
            continue
        target["props"] = {
            **(target.get("props") or {}),
            "deprecated": {"reason": f["reason"], "decision": f["decision"]},
        }
        marked += 1
    return {"deprecated": marked, "orphans": orphans}


__all__ = ["CURATION_FACTS", "CURATION_VERSION", "apply", "bind", "curation_item", "is_curation", "split"]
