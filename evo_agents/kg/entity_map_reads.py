"""The reads of a graph store that the entity map run makes as the run's sink (``evo_agents.hub.kg_entity_map_run``):
rows of build ``b`` that a reader up to a ceiling (level, location) and the store's audience sees. The SQL of the
store lives with the store, under evo_agents/kg; the hub module turns the rows into what the agent reads.

A ceiling of None sees nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

from evo_agents.kg.store_filters import LIVE, SEEN


def _visible(ceiling: tuple[int, int] | None) -> tuple[str, dict]:
    if ceiling is None:
        return "0 = 1", {}
    level = "COALESCE(label_lvl, 0) <= :lvl AND COALESCE(label_loc, 0) <= :loc"
    return f"{SEEN} AND {level}", {"lvl": ceiling[0], "loc": ceiling[1]}


def node_rows(store, b: int, ceiling: tuple[int, int] | None, kinds=()) -> Iterator[tuple]:
    """(node_id, kind, name, props) of the live nodes of build ``b`` of ``kinds`` (every kind when empty) the reader
    sees."""
    seen, args = _visible(ceiling)
    args["b"] = b
    where = ""
    if kinds:
        where = f" AND kind IN ({', '.join(f':k{n}' for n in range(len(kinds)))})"
        args |= {f"k{n}": kind for n, kind in enumerate(kinds)}
    yield from store.db.execute(f"SELECT node_id, kind, name, props FROM nodes WHERE {LIVE} AND {seen}{where}", args)


def edge_rows(store, b: int, ceiling: tuple[int, int] | None, rel: str) -> Iterator[tuple[str, str]]:
    """(src, dst) of the live edges of build ``b`` with ``rel`` the reader sees."""
    seen, args = _visible(ceiling)
    args |= {"b": b, "rel": rel}
    yield from store.db.execute(f"SELECT src, dst FROM edges WHERE rel = :rel AND {LIVE} AND {seen}", args)


def item_rows(store, b: int, ceiling: tuple[int, int] | None) -> Iterator[tuple]:
    """(item_id, rev, hash, visible) of the live whole-item units of build ``b`` by item id, visible 1 when the reader
    sees the unit and 0 when it is above the ceiling."""
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
    try:
        return store.db.execute(
            "SELECT m.cluster_id, c.level, m.node_id FROM cluster_members m JOIN clusters c ON c.cluster_id ="
            " m.cluster_id AND c.tx_from <= :b AND (c.tx_to IS NULL OR c.tx_to > :b) WHERE m.tx_from <= :b AND"
            " (m.tx_to IS NULL OR m.tx_to > :b) ORDER BY m.cluster_id, m.node_id",
            {"b": b},
        ).fetchall()
    except Exception:  # a graph built without clusters has no such tables
        return []
