"""The reads of a graph that the entity map run makes as the run's sink (``evo_agents.hub.kg_entity_map_run``): rows
of the one build of a snapshot view (``evo_agents.kg.snapshot.SnapshotView``) that a reader up to a ceiling (level,
location) and the view's audience sees. The hub module turns the rows into what the agent reads.

A ceiling of None sees nothing. A view holds one build, the one read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from evo_agents.kg.graph_model import within


def _under(label, ceiling: tuple[int, int] | None) -> bool:
    return ceiling is not None and (label[0] or 0) <= ceiling[0] and (label[1] or 0) <= ceiling[1]


def node_rows(view, ceiling: tuple[int, int] | None, kinds=()) -> Iterator[tuple]:
    """(node_id, kind, name, props) of the nodes of ``view`` of ``kinds`` (every kind when empty) the reader sees."""
    wanted = set(kinds)
    for n in view.snap.nodes:
        if (not wanted or n["kind"] in wanted) and view.sees(n) and _under(n["label"], ceiling):
            yield n["id"], n["kind"], n["name"], json.dumps(n["props"])


def edge_rows(view, ceiling: tuple[int, int] | None, rel: str) -> Iterator[tuple[str, str]]:
    """(src, dst) of the edges of ``view`` with ``rel`` the reader sees."""
    for e in view.snap.edges:
        if e["rel"] == rel and view.sees(e) and _under(e["label"], ceiling):
            yield e["src"], e["dst"]


def item_rows(view, ceiling: tuple[int, int] | None) -> Iterator[tuple]:
    """(item_id, rev, hash, visible) of the whole-item units of ``view`` by item id, visible 1 when the reader sees the
    unit and 0 when it is above the ceiling."""
    for row in sorted((r for r in view.snap.units.values() if r[2] == ""), key=lambda r: r[1]):
        seen = within(view.reader, row[7]) and _under((row[8], row[9]), ceiling)
        yield row[1], row[3], row[4], 1 if seen else 0


def cluster_rows(view) -> list[tuple]:
    """(cluster_id, level, node_id) of each member of a cluster of ``view``, by cluster and node; empty for a graph
    built without clusters."""
    found = view.snap.found
    if found is None:
        return []
    # a node's pair holds None where it is in no cluster of a level
    return sorted(
        (c, found.clusters[c]["level"], nid) for nid, pair in found.members.items() for c in pair if c in found.clusters
    )
