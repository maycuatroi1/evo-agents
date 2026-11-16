"""The nodes of a snapshot view (``evo_agents.kg.snapshot.SnapshotView``) with their labels and whether the view's
reader sees them, for a caller that decides with a ceiling of its own what a reader may read (the hub's starter
questions, ``evo_agents.hub.kg_questions``)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Labelled:
    node_id: str
    props: dict
    level: int
    location: int
    seen: bool  # whether the view's reader (its audience) sees the node

    def within(self, ceiling: tuple[int, int] | None) -> bool:
        """Whether a reader up to ``ceiling`` (level, location) reads the node; None reads nothing."""
        return ceiling is not None and self.seen and self.level <= ceiling[0] and self.location <= ceiling[1]


def labelled_nodes(view, kinds: Sequence[str] = (), node_id: str | None = None) -> Iterator[Labelled]:
    """The nodes of the one build of ``view`` (a ``SnapshotView``): of ``kinds`` (every kind when empty), or the one
    ``node_id`` names."""
    wanted = set(kinds)
    for n in view.snap.nodes:
        if (n["id"] == node_id) if node_id is not None else (not wanted or n["kind"] in wanted):
            label = n["label"]
            yield Labelled(n["id"], n["props"], label[0] or 0, label[1] or 0, view.sees(n))
