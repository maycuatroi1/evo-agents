"""The reads of a graph store that the entity map run makes as the run's sink (``evo_agents.hub.kg_entity_map_run``):
rows of build ``b`` that a reader up to a ceiling (level, location) and the store's audience sees. The SQL of the
store lives with the store, under evo_agents/kg; the hub module turns the rows into what the agent reads.

A ceiling of None sees nothing. ``store`` may be a view of a snapshot (``evo_agents.kg.snapshot.SnapshotView``), whose
rows are those of its one build: each read then gives what the store's SQL gives.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from evo_agents.kg.store_filters import LIVE, SEEN, within


def _under(label, ceiling: tuple[int, int] | None) -> bool:
    return ceiling is not None and (label[0] or 0) <= ceiling[0] and (label[1] or 0) <= ceiling[1]


def _visible(ceiling: tuple[int, int] | None) -> tuple[str, dict]:
    if ceiling is None:
        return "0 = 1", {}
    level = "COALESCE(label_lvl, 0) <= :lvl AND COALESCE(label_loc, 0) <= :loc"
    return f"{SEEN} AND {level}", {"lvl": ceiling[0], "loc": ceiling[1]}


def node_rows(store, b: int, ceiling: tuple[int, int] | None, kinds=()) -> Iterator[tuple]:
    """(node_id, kind, name, props) of the live nodes of build ``b`` of ``kinds`` (every kind when empty) the reader
    sees."""
    snap = getattr(store, "snap", None)
    if snap is not None:
        wanted = set(kinds)
        for n in snap.nodes:
            if (not wanted or n["kind"] in wanted) and store.sees(n) and _under(n["label"], ceiling):
                yield n["id"], n["kind"], n["name"], json.dumps(n["props"])
        return
    seen, args = _visible(ceiling)
    args["b"] = b
    where = ""
    if kinds:
        where = f" AND kind IN ({', '.join(f':k{n}' for n in range(len(kinds)))})"
        args |= {f"k{n}": kind for n, kind in enumerate(kinds)}
    yield from store.db.execute(f"SELECT node_id, kind, name, props FROM nodes WHERE {LIVE} AND {seen}{where}", args)


def edge_rows(store, b: int, ceiling: tuple[int, int] | None, rel: str) -> Iterator[tuple[str, str]]:
    """(src, dst) of the live edges of build ``b`` with ``rel`` the reader sees."""
    snap = getattr(store, "snap", None)
    if snap is not None:
        for e in snap.edges:
            if e["rel"] == rel and store.sees(e) and _under(e["label"], ceiling):
                yield e["src"], e["dst"]
        return
    seen, args = _visible(ceiling)
    args |= {"b": b, "rel": rel}
    yield from store.db.execute(f"SELECT src, dst FROM edges WHERE rel = :rel AND {LIVE} AND {seen}", args)


def item_rows(store, b: int, ceiling: tuple[int, int] | None) -> Iterator[tuple]:
    """(item_id, rev, hash, visible) of the live whole-item units of build ``b`` by item id, visible 1 when the reader
    sees the unit and 0 when it is above the ceiling."""
    snap = getattr(store, "snap", None)
    if snap is not None:
        for row in sorted((r for r in snap.units.values() if r[2] == ""), key=lambda r: r[1]):
            seen = within(store.reader, row[7]) and _under((row[8], row[9]), ceiling)
            yield row[1], row[3], row[4], 1 if seen else 0
        return
    seen, args = _visible(ceiling)
    args["b"] = b
    query = (
        f"SELECT item_id, rev, hash, CASE WHEN {seen} THEN 1 ELSE 0 END FROM units"
        f" WHERE anchor = '' AND {LIVE} ORDER BY item_id"
    )
    yield from store.db.execute(query, args)


def cluster_rows(store, b: int) -> list[tuple]:
    """(cluster_id, level, node_id) of each live member of a live cluster of build ``b``, by cluster and node; empty
    for a graph built without clusters, which has no such tables."""
    snap = getattr(store, "snap", None)
    if snap is not None:
        found = snap.found
        if found is None:
            return []
        members = ((c, found.clusters[c]["level"], nid) for nid, pair in found.members.items() for c in pair)
        return sorted(m for m in members if m[0] in found.clusters)
    try:
        return store.db.execute(
            "SELECT m.cluster_id, c.level, m.node_id FROM cluster_members m JOIN clusters c ON c.cluster_id ="
            " m.cluster_id AND c.tx_from <= :b AND (c.tx_to IS NULL OR c.tx_to > :b) WHERE m.tx_from <= :b AND"
            " (m.tx_to IS NULL OR m.tx_to > :b) ORDER BY m.cluster_id, m.node_id",
            {"b": b},
        ).fetchall()
    except Exception:  # a graph built without clusters has no such tables
        return []
