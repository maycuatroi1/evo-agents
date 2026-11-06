"""A named graph of a project: a set of its sources, read from the project's one build (``docs/kg.md``, Named graphs).

A project's graph is built once from every source. A named graph narrows what the kg tools return to the nodes and
edges of some of those sources, at query time: no store, build or artifact of its own. ``GraphScope`` says whether a
node or an edge of a build is of the named graph's sources:

- a node is when its readable id starts with one of the sources (``docs:doc:runbook``, ``upload.notes:...``), or when
  one of the units it was read from (``where_prov``) or derived from (``derivations``) belongs to one: a requirement
  that a document of the source mentions, a source's own node. A node with no unit of the sources is left out, so a
  named graph only ever narrows;
- an edge is when both its nodes are.

The label and audience checks of the session still apply first (``evo_agents.kg.serve_view``): the scope is one more
condition, never a widening. ``default`` names every source of the project, and so no scope at all.
"""

from __future__ import annotations

from evo_agents.kg.refs import readable

DEFAULT_GRAPH = "default"  # the implicit graph of every source of a project


class GraphScope:
    """Which nodes and edges of one build a named graph of ``sources`` holds; answers are cached per build."""

    def __init__(self, name: str, sources: frozenset[str] | set[str] | tuple[str, ...] | list[str]):
        self.name = name
        self.sources = frozenset(s.lower() for s in sources)
        self._store = None
        self._build: int | None = None
        self._nodes: dict[str, bool] = {}
        self._units: dict[str, str | None] = {}

    def bind(self, store, b: int) -> None:
        """Read build ``b`` of ``store`` from now on; the cache of another build is dropped."""
        if store is not self._store or b != self._build:
            self._store, self._build = store, b
            self._nodes, self._units = {}, {}

    def _named(self, source: str | None) -> bool:
        return isinstance(source, str) and source.lower() in self.sources

    def _unit_source(self, unit_id: str) -> str | None:
        """The source of a unit: as the store holds it, else the part of its id before the first colon (a unit the
        reader's audience does not hold still names its source)."""
        if unit_id not in self._units:
            row = self._store.unit(unit_id, self._build) if self._store is not None else None
            source = row.get("source") if row else None
            self._units[unit_id] = source if isinstance(source, str) and source else unit_id.partition(":")[0]
        return self._units[unit_id]

    def holds_id(self, node_id: str) -> bool:
        """Whether the node ``node_id`` is of the named graph's sources (see the module)."""
        found = self._nodes.get(node_id)
        if found is not None:
            return found
        found = self._named(readable(node_id).partition(":")[0])
        if not found and self._store is not None:
            units = [w["unit"] for w in self._store.where(node_id, self._build)]
            units += [u for d in self._store.derivations(node_id, self._build) for u in d["units"]]
            found = any(self._named(self._unit_source(u)) for u in units if isinstance(u, str))
        self._nodes[node_id] = found
        return found

    def holds_node(self, node: dict) -> bool:
        """``holds_id`` of a node row, which also counts the repo its properties name (a file's ``repo``)."""
        props = node.get("props") or {}
        if self._named(props.get("repo")):
            return True
        return self.holds_id(node["id"])

    def holds(self, element: dict) -> bool:
        """Whether a node or an edge row (one with ``src`` and ``dst``) is of the named graph."""
        if "src" in element and "dst" in element:
            return self.holds_id(element["src"]) and self.holds_id(element["dst"])
        return self.holds_node(element)
