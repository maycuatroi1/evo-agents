"""The nodes of a graph with their labels and whether the store's reader sees them, for a caller that decides with a
ceiling of its own what a reader may read (the hub's starter questions, ``evo_agents.hub.kg_questions``)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from evo_agents.kg.store import Store
from evo_agents.kg.store_filters import LIVE, SEEN


@dataclass(frozen=True)
class Labelled:
    node_id: str
    props: dict
    level: int
    location: int
    seen: bool  # whether the store's reader (its audience) sees the node

    def within(self, ceiling: tuple[int, int] | None) -> bool:
        """Whether a reader up to ``ceiling`` (level, location) reads the node; None reads nothing."""
        return ceiling is not None and self.seen and self.level <= ceiling[0] and self.location <= ceiling[1]


def labelled_nodes(store: Store, kinds: Sequence[str] = (), node_id: str | None = None) -> Iterator[Labelled]:
    """The live nodes of the latest ready build of ``store``: of ``kinds``, or the one ``node_id`` names."""
    b = store.latest_ready()
    if b is None:
        return
    args: dict = {"b": b}
    if node_id is not None:
        where, args["n"] = "node_id = :n", node_id
    else:
        where = f"kind IN ({', '.join(f':k{n}' for n in range(len(kinds)))})" if kinds else "1 = 1"
        args |= {f"k{n}": kind for n, kind in enumerate(kinds)}
    query = (
        f"SELECT node_id, props, label_lvl, label_loc, CASE WHEN {SEEN} THEN 1 ELSE 0 END FROM nodes"
        f" WHERE {where} AND {LIVE}"
    )
    for found, props, level, location, seen in store.db.execute(query, args):
        yield Labelled(found, json.loads(props or "{}"), level or 0, location or 0, bool(seen))
