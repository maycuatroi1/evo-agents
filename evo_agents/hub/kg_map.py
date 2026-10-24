"""The cluster map of a project's knowledge graph as one reader sees it: its level-1 clusters, one cluster with its
subclusters, members and neighbours, and the compact whole graph the map's "all nodes and edges" view draws.

A build stores its clusters over the nodes every reader of the project may read (``evo_agents.kg.clusters``), but a
reader below that sees fewer, so nothing stored is shown as it is. Every member goes through ``Session.visible`` and
everything shown is counted again over the visible members alone: a cluster's size and kinds, the weight of a link
between two clusters (the visible edges joining their visible members, as clusters project them), and a cluster's
label, its c-TF-IDF words recomputed over the names and paths of visible members against the other visible clusters
of its level. A cluster with no visible member is absent, so a reader learns neither its id, nor its label, nor its
size. A build without clusters, and a reader who sees none, get an empty map and the reason.

The whole graph is every visible node, at most a cap of them by id, and every visible edge between two of them, as
index pairs into the node list, with the level-1 cluster each node counts in (a Symbol in its file's, a Section in
its document's), so the map can colour it.

Blocking, like ``evo_agents.hub.kg_web``: called from a WebSession method in the api's thread.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg.store import Store

WHOLE_NODES = 20_000  # most nodes the whole-graph view takes
NO_VISIBLE = "no cluster of this graph has a member you can read"
NO_CLUSTERS = "this build has no clusters"

Visible = Callable[[tuple], bool]


@dataclass
class View:
    """The clusters of build b as one reader sees them."""

    reason: str | None = None
    nodes: list[dict] = field(default_factory=list)  # visible members, by id
    clusters: dict[str, dict] = field(default_factory=dict)  # visible clusters of both levels
    links: dict[tuple[int, str, str], int] = field(default_factory=dict)  # (level, a, b), a < b: visible edges
    pairs: dict[tuple[str, str], int] = field(default_factory=dict)  # (member, member), sorted: visible edges


def view(store: Store, b: int, visible: Visible) -> View:
    """The visible clusters of build b, recounted and renamed over their visible members."""
    stored = store.clusters(b)
    if stored is None:
        coverage = store.build_row(b)["coverage"].get("clusters") or {}
        return View(reason=coverage.get("reason") or NO_CLUSTERS)
    nodes = [n for n in store.cluster_nodes(b) if n["cluster"] in stored.clusters and visible(n["label"])]
    if not nodes:
        return View(reason=NO_VISIBLE)
    member = {n["id"]: n for n in nodes}
    out = View(nodes=nodes)
    for level, key in ((1, "cluster"), (2, "sub")):
        groups: dict[str, list[dict]] = defaultdict(list)
        for n in nodes:
            if n[key] is not None and n[key] in stored.clusters:
                groups[n[key]].append(n)
        names = kg_clusters.labels({cid: [(n["name"], n["path"]) for n in ms] for cid, ms in groups.items()})
        for cid, ms in sorted(groups.items()):
            out.clusters[cid] = {
                "id": cid,
                "level": level,
                "parent": ms[0]["cluster"] if level == 2 else None,
                "label": names[cid],
                "size": len(ms),
                "kinds": dict(sorted(Counter(n["kind"] for n in ms).items())),
            }
    pairs: Counter = Counter()
    for a, z, edge, src, dst, count in store.member_links(b, kg_clusters.STRUCTURAL):
        if a in member and z in member and visible(edge) and visible(src) and visible(dst):
            pairs[(a, z) if a < z else (z, a)] += count
    links: Counter = Counter()
    for (a, z), w in pairs.items():
        for level, key in ((1, "cluster"), (2, "sub")):
            ca, cz = member[a][key], member[z][key]
            if ca in out.clusters and cz in out.clusters and ca != cz:
                links[(level, *sorted((ca, cz)))] += w
    out.pairs = dict(pairs)
    out.links = dict(links)
    return out


def _summary(found: View, cid: str) -> dict:
    c = found.clusters[cid]
    subs = sum(1 for s in found.clusters.values() if s["parent"] == cid)
    return {**c, "subclusters": subs}


def _neighbours(found: View, cid: str) -> list[dict]:
    level = found.clusters[cid]["level"]
    weights: Counter = Counter()
    for (lvl, a, z), w in found.links.items():
        if lvl == level and cid in (a, z):
            weights[z if a == cid else a] += w
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"id": other, "label": found.clusters[other]["label"], "size": found.clusters[other]["size"], "weight": w}
        for other, w in ordered
    ]


def _member(n: dict) -> dict:
    return {"id": n["id"], "kind": n["kind"], "name": n["name"], "cluster": n["cluster"], "sub": n["sub"]}


def _edges(found: View, ids: list[str]) -> list[list[int]]:
    """The visible member edges between two of ``ids``, as [index, index, weight] into ``ids``."""
    index = {nid: i for i, nid in enumerate(ids)}
    return [[index[a], index[z], w] for (a, z), w in sorted(found.pairs.items()) if a in index and z in index]


def cluster_map(found: View, with_nodes: bool) -> dict:
    """The level-1 clusters, largest first, the links between them, and (``with_nodes``) every visible member with
    its clusters and the visible edges between members."""
    top = sorted((c for c in found.clusters.values() if c["level"] == 1), key=lambda c: (-c["size"], c["id"]))
    links = sorted(
        ({"a": a, "b": z, "weight": w} for (level, a, z), w in found.links.items() if level == 1),
        key=lambda link: (-link["weight"], link["a"], link["b"]),
    )
    nodes = [_member(n) for n in found.nodes] if with_nodes else []
    edges = _edges(found, [n["id"] for n in found.nodes]) if with_nodes else []
    return {
        "computed": not found.reason,
        "reason": found.reason,
        "clusters": [_summary(found, c["id"]) for c in top],
        "links": links,
        "nodes": nodes,
        "edges": edges,
    }


def cluster_detail(found: View, cid: str) -> dict | None:
    """One visible cluster of either level: its subclusters, its visible members, the visible edges between them and
    the clusters of its level its members' edges reach; None when no member of it is visible."""
    if cid not in found.clusters:
        return None
    level = found.clusters[cid]["level"]
    key = "cluster" if level == 1 else "sub"
    members = [n for n in found.nodes if n[key] == cid]
    subs = sorted((c for c in found.clusters.values() if c["parent"] == cid), key=lambda c: (-c["size"], c["id"]))
    return {
        "cluster": _summary(found, cid),
        "subclusters": [_summary(found, c["id"]) for c in subs],
        "members": [_member(n) for n in members],
        "edges": _edges(found, [n["id"] for n in members]),
        "neighbours": _neighbours(found, cid),
    }


def whole(store: Store, b: int, visible: Visible, limit: int) -> dict:
    """Every visible node, at most ``limit`` by id, with the level-1 cluster it counts in, and the visible edges between
    two of them as [source index, target index, type index into ``rels``]."""
    found = view(store, b, visible)
    top = {n["id"]: n["cluster"] for n in found.nodes}
    nodes, edges = store.live_graph(b)
    shown = [n for n in nodes if visible(n["label"])]
    truncated = len(shown) > limit
    shown = shown[:limit]
    index = {n["id"]: i for i, n in enumerate(shown)}
    rels: dict[str, int] = {}
    out_edges = []
    for src, rel, dst, label in edges:
        if src in index and dst in index and visible(label):
            out_edges.append([index[src], index[dst], rels.setdefault(rel, len(rels))])
    return {
        "nodes": [
            {"id": n["id"], "kind": n["kind"], "name": n["name"], "cluster": top.get(n["owner"] or n["id"])}
            for n in shown
        ],
        "rels": list(rels),
        "edges": out_edges,
        "truncated": truncated,
    }
