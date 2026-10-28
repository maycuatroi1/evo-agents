"""The review queue of a project's knowledge graph as one reader sees it (``evo_agents.kg.review``), and the open
items of it per rule that the night's ``kg_health`` counts.

A build with clusters stores its review items over every node; a reader sees an item only through its nodes
(``review.visible_item``): an item about a node the reader cannot see is absent, and an item about several nodes
shows those the reader can see and counts them alone. An item about an edge needs the edge visible too, and an
item's fix (``detail.fix``) every node and edge it names. Each item
shown carries the level-1 cluster of its first visible node, labelled as the reader's map labels it
(``evo_agents.hub.kg_map``), and its weight, the most visible edges any of its visible nodes has, which orders the
queue. Dismissals live on the hub (``kg_review_dismissals``) by fingerprint; this module only reads the graph.

Blocking, like ``evo_agents.hub.kg_map``: called from a WebSession method in the api's thread, or in the measure's.
"""

from __future__ import annotations

from collections.abc import Callable

from evo_agents.hub import kg_map
from evo_agents.kg import review
from evo_agents.kg.store import Store, edge_id

NO_REVIEW = "this build has no review queue: it has no clusters"

Visible = Callable[[tuple], bool]
Degrees = Callable[[Store, list[str], int], dict[str, int]]  # visible edges of each node of ids at build b


def visible_items(store: Store, b: int, visible: Visible) -> list[dict]:
    """The stored items of build b the reader sees, cut to what they see, as ``review.visible_item`` makes them."""
    items = store.review_items(b)
    if not items:
        return []
    ids = {nid for item in items for nid in item["evidence"]}
    ids |= {nid for item in items for nid in review.fix_nodes(item["detail"].get("fix") or [])}
    labels = store.labels(ids, b)
    out = []
    for item in items:
        shown = review.visible_item(item, labels, visible)
        if shown is None:
            continue
        edge = shown["detail"].get("edge")
        if edge is not None:
            label = store.edge_label(edge, b)
            if label is None or not visible(label):
                continue
        fix = shown["detail"].get("fix")
        if fix and not all(_edge_visible(store, b, visible, *e) for e in review.fix_edges(fix)):
            shown["detail"].pop("fix")
        out.append(shown)
    return out


def _edge_visible(store: Store, b: int, visible: Visible, src: str, rel: str, dst: str) -> bool:
    label = store.edge_label(edge_id(src, rel, dst), b)
    return label is not None and visible(label)


def queue(store: Store, b: int, visible: Visible, degrees: Degrees) -> dict:
    """Build b's review queue as the reader sees it: ``items``, most important first within each rule (RULES order),
    each with its nodes (id, kind, name), cluster and cluster label; and ``reason`` when there is none to show."""
    stored = store.review_items(b)
    if not stored and store.clusters(b) is None:
        return {"computed": False, "reason": NO_REVIEW, "items": []}
    items = visible_items(store, b, visible)
    ids = sorted({nid for item in items for nid in item["evidence"]})
    nodes = store.nodes(ids, b)
    weight = degrees(store, ids, b) if ids else {}
    found = kg_map.view(store, b, visible)
    rank = {rule: i for i, rule in enumerate(review.RULES)}
    out = []
    for item in items:
        cluster = found.clusters.get(item["cluster"]) if item["cluster"] else None
        detail = {k: v for k, v in item["detail"].items() if k != "label"}
        out.append(
            {
                "fingerprint": item["fingerprint"],
                "rule": item["rule"],
                "action": item["action"],
                "cluster": cluster["id"] if cluster else None,
                "cluster_label": cluster["label"] if cluster else None,
                "nodes": [
                    {"id": nid, "kind": nodes[nid]["kind"], "name": nodes[nid]["name"]}
                    for nid in item["evidence"]
                    if nid in nodes
                ],
                "detail": detail,
                "weight": max((weight.get(nid, 0) for nid in item["evidence"]), default=0),
            }
        )
    out.sort(key=lambda i: (rank.get(i["rule"], len(rank)), -i["weight"], i["fingerprint"]))
    return {"computed": True, "reason": None, "items": out}


def open_counts(store: Store, b: int, visible: Visible, dismissed: set[str]) -> dict[str, int]:
    """Per rule, the items of build b the reader sees that no one dismissed."""
    items = [item for item in visible_items(store, b, visible) if item["fingerprint"] not in dismissed]
    return review.counts(items)
