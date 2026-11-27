"""What one reader of a graph may see: the visible set V_o of a build, as every read of it checks it.

A reader is a clearance (level, location) and an audience, the principals it is (``evo_agents.schema.policy``). An
element shows when its label (level, location, integrity and, from the build's rows, audience) is at most the
clearance and its audience within the reader's (``Visibility.label``). A node a decision deprecated
(``evo_agents.kg.pipeline.curation``) shows only when a call asks for it, and a call that names a graph sees only the
nodes and edges of its sources (``evo_agents.kg.graph_scope``). When the highest label of the build is within the
clearance and its audiences within the reader's, every element is visible (``sees_all``) and no label is compared.

The kg_* tools of a session (``evo_agents.kg.serve``), the hub's web reads of a graph (``evo_agents.hub.kg_web``) and
the searches of a snapshot (``evo_agents.kg.snapshot_search``) all read through one: the session makes the one of a
call (``Visibility.asking``) and the one it reads a store with (``Visibility.reading``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from evo_agents.kg.graph_scope import GraphScope
from evo_agents.schema.policy import audience_below


@dataclass(frozen=True)
class Visibility:
    """What a reader sees: ``clearance`` (level, location) and ``audience``; ``sees_all`` when the build it reads holds
    nothing above them, ``deprecated`` when a call shows the deprecated nodes and edges too, ``scope`` the named graph
    it reads, None for every source. A reader with no clearance (a session with an error) sees only what ``sees_all``
    lets through."""

    clearance: tuple[int, int] | None = None
    audience: frozenset[str] = frozenset()
    sees_all: bool = False
    deprecated: bool = False
    scope: GraphScope | None = None

    @classmethod
    def everything(cls) -> Visibility:
        """A reader that sees every element of a build, deprecated ones apart (the benches, the tests)."""
        return cls(sees_all=True)

    def label(self, label: tuple) -> bool:
        """Whether an element labelled ``label`` (level, location, integrity and, from the build's rows, audience)
        shows."""
        if self.sees_all:
            return True
        if self.clearance is None:
            return False
        level, location, *_rest = label
        audience = label[3] if len(label) > 3 else frozenset()
        return level <= self.clearance[0] and location <= self.clearance[1] and audience_below(audience, self.audience)

    def seen(self, x: dict) -> bool:
        """``label`` of a node or an edge row, not deprecated by a decision unless asked for, and of the named graph
        the reader reads (``scoped``)."""
        return (
            (self.sees_all or self.label(x["label"]))
            and (self.deprecated or "deprecated" not in (x.get("props") or {}))
            and self.scoped(x)
        )

    def scoped(self, x: dict) -> bool:
        """Whether a node or an edge row is of the named graph the reader reads; any one without a named graph."""
        return self.scope is None or self.scope.holds(x)

    def shows(self, node: dict) -> bool:
        """``label`` of a node row, and of the named graph the reader reads; deprecated or not."""
        return (self.sees_all or self.label(node["label"])) and self.scoped(node)

    def holds_audience(self, node: dict) -> bool:
        """Whether the reader's audience holds the audience of ``node``, whatever its level and location."""
        label = node["label"]
        return audience_below(label[3] if len(label) > 3 else frozenset(), self.audience)

    def asking(self, *, deprecated: bool = False, scope: GraphScope | None = None) -> Visibility:
        """The reader as one call asks: the deprecated nodes too or not, ``scope`` the named graph it reads; nothing
        is known yet of the build it reads, so ``sees_all`` is off."""
        return replace(self, sees_all=False, deprecated=deprecated, scope=scope)

    def reading(self, store) -> Visibility:
        """The reader as it reads ``store`` (a ``SnapshotView``), which is set to read as this reader: its audience,
        its clearance for what agents asked, and the clearance as the ceiling of the edges it pages through unless the
        reader sees all. ``sees_all`` when the store's highest label and audiences are within the reader's; never with
        a named graph, which leaves out nodes the label lets through."""
        clearance = self.clearance
        assert clearance is not None, "a reader without a clearance reads no store"
        store.reader = self.audience
        store.asked_clearance = clearance  # what agents asked before reading a node, under this clearance only
        top = store.top_label()
        within = top is not None and top[0] <= clearance[0] and top[1] <= clearance[1]
        sees_all = within and audience_below(store.top_audience(), self.audience)
        store.ceiling = None if sees_all else clearance  # edges above it are left out before the limit
        if self.scope is not None:
            self.scope.bind(store)
            sees_all = False
        return replace(self, sees_all=sees_all)
