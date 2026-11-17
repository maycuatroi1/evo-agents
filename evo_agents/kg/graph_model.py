"""The graph a build assembles (``Graph``) and the rows it gives each table of a graph, keyed and labelled as the hub's
tables and its snapshots keep them (``evo_agents.kg.graph_rows``, ``evo_agents.kg.snapshot``): nodes, edges, units,
provenance, clusters, the review queue and the full-text rows. Nothing here reads or writes a file."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from evo_agents.kg import search as text_search
from evo_agents.kg.policy import TOP_AUDIENCE, audience_below, audience_join, audience_of
from evo_agents.kg.protocol import canonical_json, sha256

# the version of the tables a build's rows fill, part of every content hash: 4: units, nodes and edges gained
# label_aud, node_fts_aud came; 3: node_fts kind and path
SCHEMA_VERSION = "4"
KEYED_KINDS = ("Symbol",)  # kinds whose props.key is unique; keep in step with the nodes_key index
NODE_COLUMNS = "node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases, label_aud"
EDGE_COLUMNS = "edge_id, src, rel, dst, props, status, conf, label_lvl, label_loc, label_int, label_aud"
UNIT_FIELDS = [
    "unit_id",
    "item_id",
    "anchor",
    "rev",
    "hash",
    "source",
    "span",
    "label_lvl",
    "label_loc",
    "label_int",
    "label_aud",
]
NODE_FIELDS = [
    "node_id",
    "kind",
    "name",
    "props",
    "status",
    "conf",
    "label_lvl",
    "label_loc",
    "label_int",
    "aliases",
    "label_aud",
]
EDGE_FIELDS = ["edge_id", "src", "rel", "dst", "props", "status", "conf"] + [
    "label_lvl",
    "label_loc",
    "label_int",
    "label_aud",
]
CLUSTER_FIELDS = ["cluster_id", "level", "parent", "label", "size", "kinds"]
REVIEW_FIELDS = ["fingerprint", "rule", "cluster_id", "evidence", "clusters", "action", "detail"]


def edge_id(src: str, rel: str, dst: str) -> str:
    return "e:" + sha256(f"{src}\x1f{rel}\x1f{dst}")[7:23]


@dataclass
class Graph:
    """An assembled graph, ready to write: plain dicts keyed by id."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    derivations: dict[str, list[dict]] = field(default_factory=dict)  # elem id -> [{units, needs}]
    where: dict[str, list[list[str]]] = field(default_factory=dict)  # elem id -> [[unit, span]]
    units: dict[str, dict] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)  # node id -> the body of its searchable text
    paths: dict[str, str] = field(default_factory=dict)  # node id -> the path it is found by, when it has one
    # node id -> the passages its long body is also indexed as (``kg.search``): [{"start", "end", "text"}], text with
    # its context first; never nodes
    passages: dict[str, list[dict]] = field(default_factory=dict)
    # file node id -> the text of its code or markup, where kg_search looks up the words of a query (``code_fts``)
    code: dict[str, str] = field(default_factory=dict)
    clusters: Clusters | None = None  # None: not computed (``evo_agents.kg.clusters``)
    mentions: dict[str, dict] = field(default_factory=dict)  # item id -> its dangling and ambiguous mentions, samples
    review: list[dict] = field(default_factory=list)  # the review items of a build with clusters (``kg.review``)
    # The deprecations a curation item declared (``evo_agents.kg.pipeline.curation``): applied to the props of the
    # nodes and edges they name before the graph is written, never written themselves.
    deprecations: list[dict] = field(default_factory=list)


@dataclass
class Clusters:
    """The clusters of a graph (``evo_agents.kg.clusters``): each cluster's level (1 or 2), parent (a level-2 cluster's
    level-1 cluster), label, size and members by kind; each member's level-1 cluster and level-2 cluster, if any; and
    the weight of the edges between two clusters of a level, keyed (level, a, b) with a < b."""

    clusters: dict[str, dict] = field(default_factory=dict)
    members: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    links: dict[tuple[int, str, str], float] = field(default_factory=dict)


def key_conflicts(graph: Graph) -> dict[tuple[str, str], list[str]]:
    """Keys that more than one node of the graph would hold, under the same predicate as the nodes_key
    index. Checked before writing, so a violation names its nodes instead of surfacing as an IntegrityError."""
    holders: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid, n in graph.nodes.items():
        key = (n.get("props") or {}).get("key")
        if n["kind"] in KEYED_KINDS and n["status"] not in (None, "proposed") and key is not None:
            holders[(n["kind"], json.dumps(key, ensure_ascii=False))].append(nid)
    return {k: sorted(ids) for k, ids in sorted(holders.items()) if len(ids) > 1}


def label_columns(label) -> dict:
    """The label columns of a row: ``label_aud`` only when the label names an audience, so a row without one keeps
    the content it had before audiences came."""
    columns = {"label_lvl": label.level, "label_loc": label.location, "label_int": label.integrity}
    return {**columns, "label_aud": json.dumps(sorted(label.audience))} if label.audience else columns


def stored_audience(text: str | None) -> frozenset[str]:
    """The audience a ``label_aud`` holds; the highest for one that cannot be read."""
    if text is None:
        return frozenset()
    try:
        return audience_of(json.loads(text))
    except ValueError:
        return TOP_AUDIENCE


def within(reader: frozenset[str], text: str | None) -> bool:
    """Whether the audience a ``label_aud`` holds is within ``reader``."""
    return audience_below(stored_audience(text), reader)


def row_content(row: dict) -> str:
    return sha256(canonical_json(row))


def graph_tables(graph: Graph) -> dict[str, dict[str, dict]]:
    """The rows ``graph`` puts in each graph table, by key: what ``diff_graph`` writes and ``kg.content`` hashes."""
    units = {uid: {"unit_id": uid, **u} for uid, u in graph.units.items()}
    derivs = {}
    for elem, ds in graph.derivations.items():
        for d in ds:
            unit_ids, needs = json.dumps(sorted(d["units"])), json.dumps(sorted(d["needs"]))
            did = sha256(f"{elem}\x1f{unit_ids}\x1f{needs}")[7:23]
            derivs[f"{elem}\x1f{did}"] = {"elem_id": elem, "deriv_id": did, "units": unit_ids, "needs": needs}
    wheres = {}
    for elem, entries in graph.where.items():
        for unit, span in entries:
            wheres[f"{elem}\x1f{unit}\x1f{span}"] = {"elem_id": elem, "unit_id": unit, "span": span}
    return {
        "units": units,
        "nodes": {nid: node_columns(nid, n) for nid, n in graph.nodes.items()},
        "edges": {eid: edge_columns(eid, e) for eid, e in graph.edges.items()},
        "derivations": derivs,
        "where_prov": wheres,
    }


def node_columns(nid: str, n: dict) -> dict:
    return {
        "node_id": nid,
        "kind": n["kind"],
        "name": n["name"],
        "props": json.dumps(n["props"], ensure_ascii=False, sort_keys=True),
        "status": n["status"],
        "conf": n["conf"],
        **label_columns(n["label"]),
        "aliases": json.dumps(n.get("aliases", [])),
    }


def edge_columns(eid: str, e: dict) -> dict:
    return {
        "edge_id": eid,
        "src": e["src"],
        "rel": e["rel"],
        "dst": e["dst"],
        "props": json.dumps(e["props"], ensure_ascii=False, sort_keys=True),
        "status": e["status"],
        "conf": e["conf"],
        **label_columns(e["label"]),
    }


def graph_stats(graph: Graph) -> dict:
    """The ``size``, ``top_label`` and ``top_audience`` a build of ``graph`` records in its ``stats``."""
    return {"size": size_of(graph), "top_label": top_label_of(graph), **top_audience_of(graph)}


def size_of(graph: Graph) -> dict:
    return {"nodes": len(graph.nodes), "edges": len(graph.edges), "units": len(graph.units)}


def top_label_of(graph: Graph) -> list[int]:
    """The highest level and the highest location among the labels of ``graph``'s nodes, edges and units."""
    pairs = [(e["label"].level, e["label"].location) for e in (*graph.nodes.values(), *graph.edges.values())]
    pairs += [(u["label_lvl"], u["label_loc"]) for u in graph.units.values()]
    return [max((p[0] for p in pairs), default=0), max((p[1] for p in pairs), default=0)]


def top_audience_of(graph: Graph) -> dict:
    """``top_audience``, the join of the audiences of ``graph``'s nodes, edges and units, when one of them has one."""
    found = frozenset()
    for audience in [e["label"].audience for e in (*graph.nodes.values(), *graph.edges.values())] + [
        stored_audience(u.get("label_aud")) for u in graph.units.values()
    ]:
        found = audience_join(found, audience)
    return {"top_audience": sorted(found)} if found else {}


def cluster_tables(found: Clusters) -> dict[str, dict[str, dict]]:
    """The rows ``found`` puts in each cluster table, by key: what a build writes and ``kg.content`` hashes."""
    links = {}
    for (level, a, c), w in found.links.items():
        link_id = f"{level}\x1f{a}\x1f{c}"
        links[link_id] = {"link_id": link_id, "level": level, "a": a, "b": c, "weight": w}
    return {
        "clusters": {
            cid: {
                "cluster_id": cid,
                "level": c["level"],
                "parent": c["parent"],
                "label": c["label"],
                "size": c["size"],
                "kinds": json.dumps(c["kinds"], sort_keys=True),
            }
            for cid, c in found.clusters.items()
        },
        "cluster_members": {
            nid: {"node_id": nid, "cluster_id": top, "sub_id": sub} for nid, (top, sub) in found.members.items()
        },
        "cluster_links": links,
    }


def review_table(items: list[dict]) -> dict[str, dict]:
    """The rows of ``review_items`` for ``items``, by fingerprint."""
    return {
        item["fingerprint"]: {
            "fingerprint": item["fingerprint"],
            "rule": item["rule"],
            "cluster_id": item["cluster"],
            "evidence": json.dumps(item["evidence"], ensure_ascii=False),
            "clusters": json.dumps(item["clusters"]),
            "action": item["action"],
            "detail": json.dumps(item["detail"], ensure_ascii=False, sort_keys=True),
        }
        for item in items
    }


def search_rows(graph: Graph) -> tuple[list[tuple], list[tuple[str, tuple]]]:
    """The full-text rows of ``graph``: every row as the digest reads it, and each keyed as ``node_fts_rows`` keeps it.
    A row of a node with an audience ends with it. A passage's row is its node's (id, kind, name, path) with the
    passage's text as body, keyed ``search.passage_key``."""
    rows: list[tuple] = []
    keyed: list[tuple[str, tuple]] = []
    for nid, n in sorted(graph.nodes.items()):
        aud = (label_columns(n["label"])["label_aud"],) if n["label"].audience else ()
        name, path = text_search.index_text(n["name"] or ""), text_search.fold(graph.paths.get(nid, ""))
        row = (nid, n["kind"], name, path, text_search.index_text(graph.texts.get(nid, ""))) + aud
        rows.append(row)
        keyed.append((nid, row))
        for p in graph.passages.get(nid, ()):
            key = text_search.passage_key(nid, p["start"], p["end"])
            passage = (nid, n["kind"], name, path, text_search.index_text(p["text"])) + aud
            rows.append((key, *passage))
            keyed.append((key, passage))
    return rows, keyed


def code_digest(code: dict[str, str]) -> str | None:
    """The digest of what ``code_fts`` holds for ``code``; None when it holds nothing."""
    return sha256(canonical_json(sorted((nid, sha256(text)) for nid, text in code.items()))) if code else None


def searched_digest(rows: list[tuple], code: str | None) -> str:
    """The digest of what the full-text index holds: its rows and the digest of its code."""
    return sha256(canonical_json(rows if code is None else {"nodes": rows, "code": code}))


def tri_row(row) -> tuple:
    """A row of ``node_tri`` from one of ``node_fts`` (rowid, node id, kind, name, path): the name without the parts
    of its camelCase words ``search.index_text`` added, which substrings find, and both in ``search.tri_text``."""
    rowid, node_id, kind, name, path = row
    name = str(name or "").split("\n", 1)[0]
    return (rowid, node_id, kind, text_search.tri_text(name), text_search.tri_text(path or ""))
