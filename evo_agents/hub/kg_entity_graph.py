"""The entity graph of a project as a reader of the hub's web pages sees it: the entities the entities step stated
(``evo_agents.kg.entities``, ids ``entity:KIND:slug``), the relations between two of them, and the documents that
mention each, in the shape of LightRAG's graph view.

- ``top``: the LIMIT entities the most documents mention (TOP_NODES by default, at most MAX_ENTITY_NODES), then the
  most relations, by name, with the relations between two of them.
- ``around``: the entities within DEPTH relations of one (1 to MAX_DEPTH), at most LIMIT of them, breadth first, the
  heaviest relation first at each step, saying how many it left out.
- ``entity``: one entity with the documents that mention it, each with the model's quote, line and description and
  a link to the document at the commit its source read, and its relations.
- ``relation``: one relation with its two entities and the documents it was read from; the quote and line are those
  of the statement whose props the relation's label reads (``evo_agents.kg.assembly._write_labelled``).
- ``code_links``: the Directories, Files and Symbols an entity points to, by ``implemented_by`` (an agent's entity map,
  ``evo_agents.kg.entity_map``, with its reason) and ``refers_to`` (its name names the code,
  ``evo_agents.kg.entity_links``), one entry per code node, each with its package, its link at the commit its source
  read and its confidence; ``code_of`` the code nodes of the entities a graph shows, for the canvas to draw.

An entity shows its type (``props.type``, the kind when the model gave none), its description (the model's summary,
else the one description its props hold), how many visible documents mention it, its degree (visible relations to
another visible entity), status and model; a relation its keywords, description, weight (``props.weight``, the sum of
the strengths read, else the one strength, else 1.0), confidence, status and model. An entity also counts its code
(``code``: the visible code nodes it points to).

Visibility: every node, edge and document goes through ``Session.seen`` of the reader's session (the label within
the grant's ceiling and audience, not deprecated by a decision), the filter of every other web read. A mention counts
only when its edge and its document are seen; a relation only when it and both its entities are; a citation of a
relation only when its document is; a code link only when its edge and its code node are, and a code node's link
only when the file it is in, or for a Directory the item its source read, is. Nothing above the reader's ceiling is
counted, named or quoted. The view of a build is computed once per artifact and reader (``kg_map.ViewCache``).
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from evo_agents.hub.kg_map import ViewCache
from evo_agents.kg.freshness import Links
from evo_agents.kg.store import Store

ENTITY_PREFIX = "entity:"
MENTIONS = "mentions"
TOP_NODES = 150  # entities the overview shows by default
MAX_ENTITY_NODES = 1000  # most entities one answer holds
MAX_DEPTH = 3
CITATIONS = 50  # documents a detail quotes at most; the count says how many there are
IMPLEMENTED_BY = "implemented_by"  # evo_agents.kg.entity_map
REFERS_TO = "refers_to"  # evo_agents.kg.entity_links
CODE_RELS = (IMPLEMENTED_BY, REFERS_TO)
CODE_KINDS = ("Directory", "File", "Symbol")
CODE_LINKS = 100  # code nodes a detail lists at most; the count says how many there are
MAX_CODE_NODES = 500  # code nodes one graph answer draws at most

Seen = Callable[[dict], bool]


@dataclass
class EntityView:
    """The entities, relations and mentions of one build a reader sees."""

    nodes: dict[str, dict] = field(default_factory=dict)
    docs: dict[str, list[dict]] = field(default_factory=dict)  # entity id -> the mentions edges seen, by document
    relations: dict[str, dict] = field(default_factory=dict)  # edge id -> edge, both ends seen
    adjacent: dict[str, list[str]] = field(default_factory=dict)  # entity id -> its relations' edge ids
    code: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # entity id -> code id -> its edges, seen
    targets: dict[str, dict] = field(default_factory=dict)  # code id -> the Directory, File or Symbol, seen
    pointers: dict[str, int] = field(default_factory=dict)  # code id -> the seen entities that point to it


VIEWS: ViewCache[EntityView] = ViewCache()


def view(store: Store, b: int, seen: Seen) -> EntityView:
    """The reader's entity view of build b."""
    nodes, edges = store.entity_graph(b, ENTITY_PREFIX)
    shown = {n["id"]: n for n in nodes if seen(n)}
    mentions = [e for e in edges if e["rel"] == MENTIONS and e["dst"] in shown and seen(e)]
    documents = store.nodes({e["src"] for e in mentions}, b)
    docs: dict[str, list[dict]] = defaultdict(list)
    for e in mentions:
        doc = documents.get(e["src"])
        if doc is not None and seen(doc):
            docs[e["dst"]].append(e)
    relations: dict[str, dict] = {}
    adjacent: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e["rel"] != MENTIONS and e["src"] in shown and e["dst"] in shown and e["src"] != e["dst"] and seen(e):
            relations[e["id"]] = e
            adjacent[e["src"]].append(e["id"])
            adjacent[e["dst"]].append(e["id"])
    code, targets = _code(store, b, seen, shown)
    pointers: dict[str, int] = defaultdict(int)
    for by_target in code.values():
        for tid in by_target:
            pointers[tid] += 1
    return EntityView(
        nodes=shown,
        docs=dict(docs),
        relations=relations,
        adjacent=dict(adjacent),
        code=code,
        targets=targets,
        pointers=dict(pointers),
    )


def _code(store: Store, b: int, seen: Seen, shown: dict[str, dict]) -> tuple[dict, dict]:
    """The code each seen entity points to: the seen edges of CODE_RELS to a seen code node, ``implemented_by``
    first, then by confidence."""
    found = [e for e in store.entity_code(b, CODE_RELS, ENTITY_PREFIX) if e["src"] in shown and seen(e)]
    nodes = store.nodes({e["dst"] for e in found}, b)
    targets: dict[str, dict] = {}
    code: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for e in found:
        t = nodes.get(e["dst"])
        if t is None or t["kind"] not in CODE_KINDS or not seen(t):
            continue
        targets[t["id"]] = t
        code[e["src"]].setdefault(t["id"], []).append(e)
    for by_target in code.values():
        for edges in by_target.values():
            edges.sort(key=lambda e: (e["rel"] != IMPLEMENTED_BY, -(e["conf"] or 0.0), e["id"]))
    return dict(code), targets


def weight(edge: dict) -> float:
    props = edge["props"]
    for key in ("weight", "strength"):
        if isinstance(props.get(key), (int, float)):
            return float(props[key])
    return 1.0


def _rank(found: EntityView, nid: str) -> tuple:
    n = found.nodes[nid]
    return (-len(found.docs.get(nid, ())), -len(found.adjacent.get(nid, ())), n["name"] or nid, nid)


def node_json(found: EntityView, n: dict, describe: Callable[[tuple], dict]) -> dict:
    props = n["props"]
    return {
        "id": n["id"],
        "kind": n["kind"],
        "name": n["name"] or n["id"],
        "type": str(props.get("type") or n["kind"]),
        "description": _description(props),
        "docs": len(found.docs.get(n["id"], ())),
        "degree": len(found.adjacent.get(n["id"], ())),
        "code": len(found.code.get(n["id"], ())),
        "status": n["status"],
        "conf": n["conf"],
        "model": _model(props),
        "label": describe(n["label"]),
    }


def edge_json(e: dict, describe: Callable[[tuple], dict]) -> dict:
    props = e["props"]
    keywords = props.get("keywords")
    return {
        "id": e["id"],
        "src": e["src"],
        "rel": e["rel"],
        "dst": e["dst"],
        "keywords": keywords if isinstance(keywords, str) and keywords else None,
        "description": _description(props),
        "weight": weight(e),
        "conf": e["conf"],
        "status": e["status"],
        "model": _model(props),
        "sources": props["sources"] if isinstance(props.get("sources"), int) else None,
        "label": describe(e["label"]),
    }


def _description(props: dict) -> str | None:
    for key in ("summary", "description"):
        if isinstance(props.get(key), str) and props[key].strip():
            return props[key]
    found = props.get("descriptions")
    if isinstance(found, list) and found:
        return "\n".join(str(d) for d in found)
    return None


def _model(props: dict) -> str | None:
    return props["model"] if isinstance(props.get("model"), str) else None


def _between(found: EntityView, ids) -> list[dict]:
    """The relations of ``found`` whose two ends are in ``ids``, by edge id."""
    ids = set(ids)
    seen_ids = {eid for nid in ids for eid in found.adjacent.get(nid, ())}
    edges = (found.relations[eid] for eid in sorted(seen_ids))
    return [e for e in edges if e["src"] in ids and e["dst"] in ids]


def top(found: EntityView, limit: int) -> dict:
    """The ``limit`` entities the most documents mention and the relations between them."""
    limit = max(1, min(int(limit), MAX_ENTITY_NODES))
    ordered = sorted(found.nodes, key=lambda nid: _rank(found, nid))
    shown = ordered[:limit]
    return {
        "nodes": shown,
        "edges": _between(found, shown),
        "total_nodes": len(found.nodes),
        "total_edges": len(found.relations),
        "truncated": len(ordered) > limit,
    }


def around(found: EntityView, focus: str, depth: int, limit: int) -> dict:
    """The entities within ``depth`` relations of ``focus``, at most ``limit``, and the relations between them."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), MAX_ENTITY_NODES))
    hop = {focus: 0}
    left_out: set[str] = set()
    frontier = deque([focus])
    while frontier:
        current = frontier.popleft()
        if hop[current] >= depth:
            continue
        steps = sorted(
            (found.relations[eid] for eid in found.adjacent.get(current, ())),
            key=lambda e: (-weight(e), _rank(found, _other(e, current))),
        )
        for e in steps:
            other = _other(e, current)
            if other in hop:
                continue
            if len(hop) >= limit:
                left_out.add(other)
                continue
            hop[other] = hop[current] + 1
            frontier.append(other)
    return {
        "nodes": list(hop),
        "hop": hop,
        "edges": _between(found, hop),
        "depth": depth,
        "limit": limit,
        "left_out": len(left_out),
        "truncated": bool(left_out),
    }


def _other(edge: dict, current: str) -> str:
    return edge["dst"] if edge["src"] == current else edge["src"]


def _document(store: Store, b: int, links: Links, doc: dict, edge_id: str | None) -> dict:
    """A document as a citation names it: id, name, path and its link at the commit its source read (the source of
    ``edge_id``'s first unit, else the document's own)."""
    source = None
    for d in store.derivations(edge_id or doc["id"], b):
        for u in d["units"]:
            unit = store.unit(u, b)
            if unit is not None:
                source = unit["source"]
                break
        if source is not None:
            break
    props = doc["props"]
    return {
        "document": doc["id"],
        "name": doc["name"] or doc["id"],
        "path": props.get("path") if isinstance(props.get("path"), str) else None,
        "uri": links.at_commit(props.get("uri"), source),
    }


def _quoted(props: dict) -> dict:
    line = props.get("line")
    return {
        "quote": props.get("evidence") if isinstance(props.get("evidence"), str) and props["evidence"] else None,
        "line": line if isinstance(line, int) else None,
    }


def entity_citations(store: Store, b: int, found: EntityView, nid: str) -> tuple[list[dict], int]:
    """The documents that mention entity ``nid`` (at most CITATIONS, by name), each with what its mention says."""
    mentions = found.docs.get(nid, [])
    documents = store.nodes({e["src"] for e in mentions}, b)
    links = Links.of(store, b)
    out = []
    for e in mentions:
        doc = documents[e["src"]]
        entry = {**_document(store, b, links, doc, e["id"]), **_quoted(e["props"])}
        entry["description"] = _description(e["props"])
        entry["model"] = _model(e["props"])
        out.append(entry)
    out.sort(key=lambda c: (c["name"], c["document"]))
    return out[:CITATIONS], len(out)


def relation_citations(store: Store, b: int, edge: dict, seen: Seen) -> tuple[list[dict], int]:
    """The documents relation ``edge`` was read from that the reader sees (at most CITATIONS, by name); the one whose
    statement the relation's props hold carries its quote and line."""
    items: dict[str, str] = {}  # document id -> the source of the unit it was read from
    for d in store.derivations(edge["id"], b):
        for u in d["units"]:
            unit = store.unit(u, b)
            if unit is not None:
                items.setdefault(unit["item"], unit["source"])
    documents = store.nodes(items, b)
    links = Links.of(store, b)
    out = []
    for item, source in items.items():
        doc = documents.get(item)
        if doc is None or not seen(doc):
            continue
        props = doc["props"]
        entry = {
            "document": doc["id"],
            "name": doc["name"] or doc["id"],
            "path": props.get("path") if isinstance(props.get("path"), str) else None,
            "uri": links.at_commit(props.get("uri"), source),
            "quote": None,
            "line": None,
            "description": None,
            "model": None,
        }
        if edge["props"].get("item") == item:
            entry.update(_quoted(edge["props"]), description=_description(edge["props"]), model=_model(edge["props"]))
        out.append(entry)
    out.sort(key=lambda c: (c["quote"] is None, c["name"], c["document"]))
    return out[:CITATIONS], len(out)


def _repo(props: dict) -> str | None:
    if isinstance(props.get("repo"), str):
        return props["repo"]
    key = props.get("key")  # a Symbol's (repo, path, qualname), evo_agents.kg.pipeline.stages.symbol_key
    return key[0] if isinstance(key, list) and key and isinstance(key[0], str) else None


def package(kind: str, path: str | None) -> str:
    """The directory a code node is in, the package a reader groups it under: a Directory's own path, a File's or a
    Symbol's parent directory, ``""`` at the repo's root."""
    if not path:
        return ""
    if kind == "Directory":
        return path
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def code_node_json(found: EntityView, t: dict, describe: Callable[[tuple], dict]) -> dict:
    """A code node as the canvas draws it: its kind, name, place and how many visible entities point to it."""
    props = t["props"]
    path = props.get("path") if isinstance(props.get("path"), str) else None
    return {
        "id": t["id"],
        "kind": t["kind"],
        "name": t["name"] or t["id"],
        "repo": _repo(props),
        "path": path,
        "package": package(t["kind"], path),
        "entities": found.pointers.get(t["id"], 0),
        "label": describe(t["label"]),
    }


def code_of(found: EntityView, entities, limit: int = MAX_CODE_NODES) -> dict:
    """The code nodes the ``entities`` shown point to, at most ``limit`` (each entity's surest first, entity by
    entity), and one edge from an entity to each of its code nodes drawn, with its surest relation."""
    drawn: dict[str, None] = {}
    edges = []
    left_out: set[str] = set()
    for nid in entities:
        by_target = found.code.get(nid, {})
        for tid in sorted(by_target, key=lambda t: (-_surest(by_target[t]), t)):
            if tid not in drawn and len(drawn) >= limit:
                left_out.add(tid)
                continue
            drawn[tid] = None
            first = by_target[tid][0]
            edges.append({"src": nid, "rel": first["rel"], "dst": tid, "conf": _surest(by_target[tid])})
    return {"nodes": list(drawn), "edges": edges, "left_out": len(left_out - drawn.keys())}


def _surest(edges: list[dict]) -> float:
    return max((e["conf"] for e in edges if isinstance(e["conf"], (int, float))), default=0.0)


def _source(store: Store, b: int, nid: str, seen: Seen) -> str | None:
    """The source of the first unit node ``nid`` was derived from whose item the reader sees."""
    for d in store.derivations(nid, b):
        for u in d["units"]:
            unit = store.unit(u, b)
            if unit is None:
                continue
            item = store.nodes([unit["item"]], b).get(unit["item"])
            if item is not None and seen(item):
                return unit["source"]
    return None


def _directory_uri(links: Links, source: str | None, path: str | None) -> str | None:
    """A Directory's page at the commit its source read: the source's file link at that commit, as a tree."""
    prefixes = links.prefixes.get(source) if source else None
    if prefixes is None or not path:
        return None
    return prefixes[1].replace("/blob/", "/tree/", 1) + path


def code_links(
    store: Store, b: int, found: EntityView, nid: str, seen: Seen, describe: Callable[[tuple], dict]
) -> tuple[list[dict], int]:
    """The code entity ``nid`` points to (at most CODE_LINKS, by package, kind and path), one entry per code node: the
    relations that link them, the agent's reason and the name that matched, the surest confidence, and the code's
    link at the commit its source read (a Symbol's file's, with its line)."""
    by_target = found.code.get(nid, {})
    targets = [found.targets[t] for t in by_target]
    files = store.nodes(
        {t["props"]["file"] for t in targets if t["kind"] == "Symbol" and isinstance(t["props"].get("file"), str)}, b
    )
    links = Links.of(store, b)
    out = []
    for t in targets:
        props = t["props"]
        edges = by_target[t["id"]]
        entry = code_node_json(found, t, describe)
        del entry["entities"]
        if t["kind"] == "Directory":
            uri = _directory_uri(links, _source(store, b, t["id"], seen), entry["path"])
        else:
            owner = files.get(props.get("file")) if t["kind"] == "Symbol" else t
            seen_owner = owner is not None and (owner is t or seen(owner))
            uri = links.node(owner)["props"].get("uri") if seen_owner else None
        reason = next((e["props"].get("reason") for e in edges if e["rel"] == IMPLEMENTED_BY), None)
        matched = next((e["props"].get("name") for e in edges if e["rel"] == REFERS_TO), None)
        line = props.get("line")
        entry.update(
            rels=sorted({e["rel"] for e in edges}),
            reason=reason if isinstance(reason, str) and reason else None,
            name_match=matched if isinstance(matched, str) and matched else None,
            conf=_surest(edges) if any(isinstance(e["conf"], (int, float)) for e in edges) else None,
            status=edges[0]["status"],
            model=_model(edges[0]["props"]),
            uri=uri if isinstance(uri, str) else None,
            line=line if isinstance(line, int) and t["kind"] == "Symbol" else None,
        )
        out.append(entry)
    order = {kind: i for i, kind in enumerate(CODE_KINDS)}
    out.sort(key=lambda c: (c["repo"] or "", c["package"], order[c["kind"]], c["path"] or "", c["name"], c["id"]))
    return out[:CODE_LINKS], len(out)


__all__ = [
    "CITATIONS",
    "CODE_LINKS",
    "CODE_RELS",
    "MAX_CODE_NODES",
    "MAX_DEPTH",
    "MAX_ENTITY_NODES",
    "TOP_NODES",
    "VIEWS",
    "EntityView",
    "around",
    "code_links",
    "code_node_json",
    "code_of",
    "edge_json",
    "entity_citations",
    "node_json",
    "package",
    "relation_citations",
    "top",
    "view",
]
