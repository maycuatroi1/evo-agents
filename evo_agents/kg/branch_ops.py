"""The operations of a draft branch of a graph (plan kg-centralized-on-hub, criterion a5), applied to the rows of its
main branch (``evo_agents.kg.snapshot.StoreRows``) without changing them.

A branch holds a list of operations, each a JSON object with ``op``:

- ``node.put``: a node (``id``, ``kind``, ``name``, ``props``), new or in place of one; a new node takes ``label``
  ([level, location]) and ``audience`` when given, else the top label of the graph, with integrity U;
- ``node.delete``: node ``id`` and every edge that ends at it;
- ``edge.put``: an edge (``src``, ``rel``, ``dst``, ``props``, ``status``, by default ``declared``) between two nodes
  the branch holds;
- ``edge.delete``: the edge ``src``, ``rel``, ``dst``;
- ``entity_map``: ``entity`` mapped to ``code``, an edge ``implemented_by`` (``rel`` may name another) declared;
- ``decision``: what a decision of ``evo_agents.hub.kg_decisions`` does to the graph: ``deprecate`` a node (``src``)
  or an edge (``src``, ``rel``, ``dst``), ``merge`` node ``dst`` into ``src`` (its edges move to ``src``, its id becomes
  an alias of it) or ``link`` two nodes (an edge declared).

``normalize`` checks an operation and gives it in one form; ``apply`` gives the rows of main with the operations
applied, in order, and a content hash of their own; ``diff`` the structural difference of two such rows; ``touched``
the nodes and edges an operation reads, which a merge compares with main (``evo_agents.hub.kg_branches``). A node a
branch adds is searchable by its name: it gets a full-text row as a build gives one."""

from __future__ import annotations

import dataclasses
import json
from hashlib import sha256

from evo_agents.kg import search as text_search
from evo_agents.kg.snapshot import StoreRows, edge_dict, edge_row, node_dict, node_row
from evo_agents.schema.policy import Label, audience_below, audience_join

OPS = ("node.put", "node.delete", "edge.put", "edge.delete", "entity_map", "decision")
DECISIONS = ("deprecate", "merge", "link")
DECLARED = "declared"
ENTITY_REL = "implemented_by"
MAX_OPS = 1000  # the most operations one call applies
MAX_PROPS_CHARS = 20_000


class OpError(ValueError):
    """An operation that is not one of OPS or lacks what it needs."""


def edge_id(src: str, rel: str, dst: str) -> str:
    """The id a store gives the edge ``src`` ``rel`` ``dst`` (``evo_agents.kg.graph_model.edge_id``)."""
    return "e:" + sha256(f"{src}\x1f{rel}\x1f{dst}".encode()).hexdigest()[:16]


def _text(op: dict, key: str, required: bool = True) -> str | None:
    value = op.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OpError(f"{op.get('op')} needs {key!r}, a non-empty string")
    return value.strip()


def _props(op: dict) -> dict:
    props = op.get("props") or {}
    if not isinstance(props, dict):
        raise OpError(f"{op.get('op')}: props must be an object")
    if len(json.dumps(props, ensure_ascii=False)) > MAX_PROPS_CHARS:
        raise OpError(f"{op.get('op')}: props past {MAX_PROPS_CHARS} characters")
    return props


def _label(op: dict) -> dict:
    out: dict = {}
    label = op.get("label")
    if label is not None:
        if not (isinstance(label, list) and len(label) == 2 and all(isinstance(x, int) and x >= 0 for x in label)):
            raise OpError(f"{op.get('op')}: label must be [level, location], two integers from 0")
        out["label"] = list(label)
    audience = op.get("audience")
    if audience is not None:
        if not (isinstance(audience, list) and all(isinstance(a, str) and a for a in audience)):
            raise OpError(f"{op.get('op')}: audience must be a list of principals")
        out["audience"] = sorted(set(audience))
    return out


def _edge_op(op: dict, name: str) -> dict:
    out = {"op": name, "src": _text(op, "src"), "rel": _text(op, "rel"), "dst": _text(op, "dst")}
    if name == "edge.put":
        status = _text(op, "status", required=False) or DECLARED
        out.update(props=_props(op), status=status, **_label(op))
    return out


def normalize(op) -> dict:
    """``op`` checked and in one form. Raises OpError."""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        raise OpError(f"an operation is an object whose op is one of {', '.join(OPS)}")
    name = op["op"]
    if name == "node.put":
        node = {"op": name, "id": _text(op, "id"), "kind": _text(op, "kind"), "name": _text(op, "name", False)}
        return {**node, "props": _props(op), **_label(op)}
    if name == "node.delete":
        return {"op": name, "id": _text(op, "id")}
    if name in ("edge.put", "edge.delete"):
        return _edge_op(op, name)
    if name == "entity_map":
        rel = _text(op, "rel", required=False) or ENTITY_REL
        return {"op": name, "entity": _text(op, "entity"), "code": _text(op, "code"), "rel": rel}
    kind = op.get("kind")
    if kind not in DECISIONS:
        raise OpError(f"a decision's kind is one of {', '.join(DECISIONS)}")
    out = {"op": name, "kind": kind, "src": _text(op, "src")}
    needs_edge = kind in ("merge", "link")
    out["dst"] = _text(op, "dst", required=needs_edge)
    out["rel"] = _text(op, "rel", required=kind == "link")
    if kind == "deprecate" and (out["rel"] is None) != (out["dst"] is None):
        raise OpError("a deprecation names a node (src) or an edge (src, rel, dst)")
    return out


def touched(op: dict) -> tuple[list[str], list[str]]:
    """(the node ids, the edge ids) whose row on main an operation reads or replaces."""
    name = op["op"]
    if name in ("node.put", "node.delete"):
        return [op["id"]], []
    if name in ("edge.put", "edge.delete"):
        return [op["src"], op["dst"]], [edge_id(op["src"], op["rel"], op["dst"])]
    if name == "entity_map":
        return [op["entity"], op["code"]], [edge_id(op["entity"], op["rel"], op["code"])]
    ends = [x for x in (op["src"], op.get("dst")) if x]
    edges = [edge_id(op["src"], op["rel"], op["dst"])] if op.get("rel") and op.get("dst") else []
    return ends, edges


class _Graph:
    """The node and edge rows of main as dicts the operations change, and the full-text rows of added nodes."""

    def __init__(self, rows: StoreRows):
        self.nodes = {r[0]: node_dict(r) for r in rows.nodes}
        self.edges = {r[0]: edge_dict(r) for r in rows.edges}
        self.own_rows = {key: row for key, row in rows.keyed if key == row[0]}
        (level, location), audience = rows.top
        self.top = (level, location)
        self.top_audience = audience
        self.texts: dict[str, tuple] = {}
        self.dropped: set[str] = set()  # nodes whose full-text rows go

    def label(self, op: dict, like: dict | None) -> tuple:
        if like is not None and "label" not in op and "audience" not in op:
            return like["label"]
        level, location = op.get("label") or self.top
        return (level, location, "U", frozenset(op.get("audience") or ()))

    def need(self, *ids: str) -> None:
        missing = [nid for nid in ids if nid not in self.nodes]
        if missing:
            raise OpError(f"no node {', '.join(missing)} on the branch")

    def put_node(self, op: dict) -> None:
        old = self.nodes.get(op["id"])
        if old is None and op.get("update"):  # it changed a node of main that left the sources since
            raise OpError(f"no node {op['id']} on the branch")
        name = op.get("name") or (old or {}).get("name") or op["id"]
        props = {**((old or {}).get("props") or {}), **op["props"]} if old else dict(op["props"])
        self.nodes[op["id"]] = {
            "id": op["id"],
            "kind": op["kind"],
            "name": name,
            "props": props,
            "status": (old or {}).get("status") or DECLARED,
            "conf": (old or {}).get("conf"),
            "label": self.label(op, old),
            "aliases": (old or {}).get("aliases") or [],
        }
        base = self.own_rows.get(op["id"])
        if base is None or base[2] != text_search.index_text(name) or base[1] != op["kind"]:
            rest = tuple(base[3:]) if base is not None else ("", "")
            self.texts[op["id"]] = (op["id"], op["kind"], text_search.index_text(name), *rest)

    def delete_node(self, nid: str) -> None:
        self.need(nid)
        del self.nodes[nid]
        self.texts.pop(nid, None)
        self.dropped.add(nid)
        for eid in [eid for eid, e in self.edges.items() if nid in (e["src"], e["dst"])]:
            del self.edges[eid]

    def put_edge(self, op: dict, src: str, rel: str, dst: str, status: str = DECLARED) -> None:
        self.need(src, dst)
        eid = edge_id(src, rel, dst)
        old = self.edges.get(eid)
        like = old or self.nodes[src]
        self.edges[eid] = {
            "id": eid,
            "src": src,
            "rel": rel,
            "dst": dst,
            "props": {**((old or {}).get("props") or {}), **(op.get("props") or {})},
            "status": op.get("status") or status,
            "conf": (old or {}).get("conf"),
            "label": self.label(op, like),
        }

    def delete_edge(self, src: str, rel: str, dst: str) -> None:
        eid = edge_id(src, rel, dst)
        if eid not in self.edges:
            raise OpError(f"no edge {src} {rel} {dst} on the branch")
        del self.edges[eid]

    def merge(self, keep: str, gone: str) -> None:
        self.need(keep, gone)
        moved = [e for e in self.edges.values() if gone in (e["src"], e["dst"])]
        aliases = sorted({*self.nodes[keep]["aliases"], gone, *self.nodes[gone]["aliases"]})
        self.delete_node(gone)
        self.nodes[keep] = {**self.nodes[keep], "aliases": aliases}
        for e in moved:
            src = keep if e["src"] == gone else e["src"]
            dst = keep if e["dst"] == gone else e["dst"]
            if src != dst:
                self.put_edge({"props": e["props"], "status": e["status"]}, src, e["rel"], dst)
                moved_to = self.edges[edge_id(src, e["rel"], dst)]
                moved_to["label"] = _label_join(moved_to["label"], e["label"])  # it keeps the label of what it holds

    def decide(self, op: dict) -> None:
        kind = op["kind"]
        if kind == "merge":
            self.merge(op["src"], op["dst"])
        elif kind == "link":
            self.put_edge({}, op["src"], op["rel"], op["dst"])
        elif op.get("rel"):
            eid = edge_id(op["src"], op["rel"], op["dst"])
            if eid not in self.edges:
                raise OpError(f"no edge {op['src']} {op['rel']} {op['dst']} on the branch")
            self.edges[eid] = {**self.edges[eid], "status": "deprecated"}
        else:
            self.need(op["src"])
            node = self.nodes[op["src"]]
            self.nodes[op["src"]] = {**node, "props": {**node["props"], "deprecated": {"by": "branch"}}}

    def run(self, op: dict) -> None:
        name = op["op"]
        if name == "node.put":
            self.put_node(op)
        elif name == "node.delete":
            self.delete_node(op["id"])
        elif name == "edge.put":
            self.put_edge(op, op["src"], op["rel"], op["dst"], op.get("status") or DECLARED)
        elif name == "edge.delete":
            self.delete_edge(op["src"], op["rel"], op["dst"])
        elif name == "entity_map":
            self.put_edge({}, op["entity"], op["rel"], op["code"])
        else:
            self.decide(op)


def _labelled(x: dict) -> dict:
    lvl, loc, integrity, audience = x["label"]
    return {**x, "label": Label(lvl, loc, integrity, frozenset(), frozenset(audience))}


def ops_hash(content_hash: str, ops: list[dict]) -> str:
    """The content hash of a graph of hash ``content_hash`` with ``ops`` applied."""
    digest = sha256(json.dumps([content_hash, ops], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f"sha256:{digest}"


def apply(
    rows: StoreRows, ops: list[dict], skipped: list[str] | None = None, hashed: list[dict] | None = None
) -> StoreRows:
    """The rows of ``rows`` with ``ops`` (normalized) applied in order; ``rows`` unchanged. Raises OpError for an
    operation that names a node or edge the branch lacks by then, unless ``skipped`` is given: such an operation is
    then left out and why goes in it. The content hash is that of ``rows`` and the operations, ``hashed`` in their
    place when given (``ops_hash``), so a branch with no operation hashes as main."""
    if not ops:
        return rows
    graph = _Graph(rows)
    for op in ops:
        try:
            graph.run(op)
        except OpError as exc:
            if skipped is None:
                raise
            skipped.append(str(exc))
    nodes = tuple(node_row(nid, _labelled(n)) for nid, n in sorted(graph.nodes.items()))
    edges = tuple(edge_row(eid, _labelled(e)) for eid, e in sorted(graph.edges.items()))
    keyed = [(k, row) for k, row in rows.keyed if row[0] not in graph.dropped and k not in graph.texts]
    keyed += [(nid, row) for nid, row in graph.texts.items() if nid in graph.nodes]
    return dataclasses.replace(
        rows,
        content_hash=ops_hash(rows.content_hash, ops if hashed is None else hashed),
        nodes=nodes,
        edges=edges,
        keyed=tuple(sorted(keyed, key=lambda kr: kr[0])),
        code={nid: text for nid, text in rows.code.items() if nid not in graph.dropped},
    )


def _side(old: dict[str, tuple], new: dict[str, tuple], shown) -> dict:
    return {
        "added": [shown(new[k]) for k in sorted(new.keys() - old.keys())],
        "changed": [
            {"before": shown(old[k]), "after": shown(new[k])}
            for k in sorted(old.keys() & new.keys())
            if old[k] != new[k]
        ],
        "removed": [shown(old[k]) for k in sorted(old.keys() - new.keys())],
    }


def _node_shown(row: tuple) -> dict:
    return {"id": row[0], "kind": row[1], "name": row[2], "props": json.loads(row[3] or "{}"), "status": row[4]}


def _edge_shown(row: tuple) -> dict:
    props = json.loads(row[4] or "{}")
    return {"id": row[0], "src": row[1], "rel": row[2], "dst": row[3], "props": props, "status": row[5]}


def diff(base: StoreRows, branch: StoreRows) -> dict:
    """The structural difference of ``branch`` from ``base``: nodes and edges added, changed (before and after) and
    removed, each by id, and their counts."""
    nodes = _side({r[0]: r for r in base.nodes}, {r[0]: r for r in branch.nodes}, _node_shown)
    edges = _side({r[0]: r for r in base.edges}, {r[0]: r for r in branch.edges}, _edge_shown)
    counts = {
        f"{what}_{side}": len(found[side]) for what, found in (("nodes", nodes), ("edges", edges)) for side in found
    }
    return {"nodes": nodes, "edges": edges, "counts": counts}


def visible(rows: StoreRows, reader: frozenset[str], ceiling: tuple[int, int] | None) -> set[str]:
    """The ids of the nodes and edges of ``rows`` a reader of audience ``reader`` reads at most ``ceiling`` (level,
    location): none for a None ceiling, a reader without a grant."""
    labels = _labels(rows)
    return {key for key, label in labels.items() if _reads(label, reader, ceiling)}


def _labels(rows: StoreRows) -> dict[str, tuple]:
    out = {r[0]: node_dict(r)["label"] for r in rows.nodes}
    out.update({r[0]: edge_dict(r)["label"] for r in rows.edges})
    return out


def _reads(label: tuple, reader: frozenset[str], ceiling: tuple[int, int] | None) -> bool:
    return (
        ceiling is not None and label[0] <= ceiling[0] and label[1] <= ceiling[1] and audience_below(label[3], reader)
    )


def changed_ids(before: StoreRows, after: StoreRows) -> set[str]:
    """The ids of the nodes and edges ``after`` adds, changes or removes from ``before``."""
    old = {r[0]: r for r in (*before.nodes, *before.edges)}
    new = {r[0]: r for r in (*after.nodes, *after.edges)}
    return {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}


def write_refusal(
    main: StoreRows, rows: StoreRows, ids, reader: frozenset[str], ceiling: tuple[int, int] | None
) -> str | None:
    """Why a writer of audience ``reader`` and grant ``ceiling`` may not make ``rows`` from ``main`` for these
    node and edge ids, None when it may: it changes a row it does not read, leaves one above its grant, or lowers a
    row's label (level, location or audience); a branch may raise a label, never lower it."""
    before, after = _labels(main), _labels(rows)
    for key in sorted(set(ids)):
        old, new = before.get(key), after.get(key)
        if old is not None and not _reads(old, reader, ceiling):
            return f"{key} is above your grant"
        if new is not None and not _reads(new, reader, ceiling):
            return f"{key} would be labelled above your grant"
        if old is not None and new is not None and _lowered(old, new):
            return f"{key} would be labelled below what it is: a branch may raise a label, not lower it"
    return None


def _label_join(a: tuple, b: tuple) -> tuple:
    """The label of a row that holds what rows of labels ``a`` and ``b`` held: the higher of each part."""
    integrity = "U" if "U" in (a[2], b[2]) else a[2]
    return (max(a[0], b[0]), max(a[1], b[1]), integrity, audience_join(frozenset(a[3]), frozenset(b[3])))


def _lowered(old: tuple, new: tuple) -> bool:
    return new[0] < old[0] or new[1] < old[1] or not audience_below(old[3], new[3])


def shown_diff(found: dict, seen: set[str]) -> dict:
    """``found`` (``diff``) without the nodes and edges whose id is not in ``seen``, its counts made again."""
    nodes, edges = (
        {side: [x for x in items if _id(x) in seen] for side, items in found[k].items()} for k in ("nodes", "edges")
    )
    counts = {f"{what}_{side}": len(part[side]) for what, part in (("nodes", nodes), ("edges", edges)) for side in part}
    return {"nodes": nodes, "edges": edges, "counts": counts}


def _id(entry: dict) -> str:
    return entry["after"]["id"] if "after" in entry else entry["id"]


def rows_of(rows: StoreRows, node_ids, edge_ids) -> dict[str, tuple | None]:
    """The rows of ``rows`` of these node and edge ids, None for one it lacks: what a merge compares."""
    nodes = {r[0]: r for r in rows.nodes}
    edges = {r[0]: r for r in rows.edges}
    out: dict[str, tuple | None] = {nid: nodes.get(nid) for nid in node_ids}
    out.update({eid: edges.get(eid) for eid in edge_ids})
    return out
