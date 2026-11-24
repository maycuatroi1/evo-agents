"""The nodes of a snapshot view (``evo_agents.kg.snapshot.SnapshotView``) with their labels and whether the view's
reader sees them, for a caller that decides with a ceiling of its own what a reader may read (the hub's starter
questions, ``evo_agents.hub.kg_questions``)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from evo_agents.kg.policy import audience_below


@dataclass(frozen=True)
class Labelled:
    node_id: str
    props: dict  # as the view's reader reads them (``SnapshotView.read``)
    level: int
    location: int
    seen: bool  # whether the view's reader (its audience) sees the node
    # of a node with guards (``evo_agents.kg.assembly``): (level, location, whether the reader's audience is within)
    # of each, one of which a reader must pass
    guards: tuple[tuple[int, int, bool], ...] = ()

    def within(self, ceiling: tuple[int, int] | None) -> bool:
        """Whether a reader up to ``ceiling`` (level, location) reads the node; None reads nothing."""
        if ceiling is None:
            return False
        if self.guards:
            return any(seen and level <= ceiling[0] and location <= ceiling[1] for level, location, seen in self.guards)
        return self.seen and self.level <= ceiling[0] and self.location <= ceiling[1]


def labelled_nodes(view, kinds: Sequence[str] = (), node_id: str | None = None) -> Iterator[Labelled]:
    """The nodes of the one build of ``view`` (a ``SnapshotView``): of ``kinds`` (every kind when empty), or the one
    ``node_id`` names."""
    wanted = set(kinds)
    for n in view.snap.nodes:
        if (n["id"] == node_id) if node_id is not None else (not wanted or n["kind"] in wanted):
            guards = tuple(
                (g[0][0] or 0, g[0][1] or 0, audience_below(g[0][3], view.reader)) for g in n.get("guards") or ()
            )
            seen = view.sees(n)
            read = view.read(n) if seen else n
            label = read["label"]
            yield Labelled(n["id"], read["props"], label[0] or 0, label[1] or 0, seen, guards)
