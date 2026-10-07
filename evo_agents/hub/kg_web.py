"""What the hub's web pages read of a project's knowledge graph: node search, one node with its neighbours, the
neighbourhood the graph view draws, and how many visible nodes of each kind the graph holds.

Each read opens the project's latest successful build the way the kg_* tools do (``evo_agents.hub.kg_graph``: the
verified artifact cache, the fallback to a cached build when the blob store does not answer) and answers with the
code of those tools: ``tool_kg_search`` and ``tool_kg_node`` of ``evo_agents.kg.serve.Session`` as they are, and a
neighbourhood that walks like ``tool_kg_context``. Every node and edge goes through ``Session.visible``, the one
filter the tools use too; only the ceiling differs. The tools read with the meet of the member's grant and a sink's
clearance, the web with the grant alone (``ProjectRules.grant_label`` of ``evo_agents.hub.access``, the label
``visible_by_grant`` holds a memory's label below).

The neighbourhood is bounded: at most MAX_HOPS steps from the node shown and at most MAX_NODES nodes, the node
itself included. Like kg_context it reads visible edges strongest status first, keeps ``in_source`` and ``part_of``
for last, and does not expand a node past the first step when it has more than HUB_DEGREE visible edges (it would
fill the view with the neighbours of everything); unlike kg_context it stops at a node count instead of a character
budget and says what it left out. Results are the structured data of the tools, never their text, so the 25,000
character cap of an MCP answer does not cut them.

Blocking: the api runs these in a thread, which also keeps the SQLite connection in the thread that made it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TypeVar

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.kg_graph import BuiltGraph, GraphCache, HubSession
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.serve import HUB_DEGREE, SEED_EDGES, STATUS_RANK, ToolError
from evo_agents.kg.store import Store

MAX_HOPS = 2
MAX_NODES = 150
SEARCH_LIMIT = 25  # kg_search's own maximum
WEB_SINK = "web"  # the name a web read gives itself in messages; it is no sink of the project

T = TypeVar("T")


class NotVisible(Exception):
    """No visible node has that id: a missing node and a hidden one look the same."""


class GraphProblem(Exception):
    """The build's artifact holds no readable graph."""


class WebSession(HubSession):
    """The tools' Session over a built graph, read with the grant's ceiling, answering with structured data."""

    def _data(self, handler: Callable[..., tuple[str, dict]], **arguments) -> dict:
        if self.error:
            raise NotVisible(self.error)
        self.read = None
        try:
            _text, data = handler(**arguments)
        except ToolError as exc:
            raise GraphProblem(str(exc)) from None
        return data

    def _visible_store(self) -> tuple[Store, int]:
        try:
            return self._store()
        except ToolError as exc:
            raise GraphProblem(str(exc)) from None

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 10) -> dict:
        """kg_search: visible nodes matching ``query``, ``kinds`` only when given."""
        if self.error:
            return {"results": []}
        return self._data(self.tool_kg_search, query=query, kinds=kinds or None, limit=limit)

    def node(self, node_id: str) -> dict:
        """kg_node: the node (by id or alias), its evidence and its visible edges. Raises NotVisible."""
        if self.error:
            raise NotVisible(node_id)
        store, b = self._visible_store()
        if self._lookup(store, node_id, b) is None:
            raise NotVisible(node_id)
        return self._data(self.tool_kg_node, id=node_id)

    def kinds(self) -> list[dict]:
        """How many visible nodes of each kind the graph holds, most first."""
        if self.error:
            return []
        store, b = self._visible_store()
        counts: dict[str, int] = {}
        for kind, level, location, integrity, count in store.kind_counts(b):
            if self.visible((level, location, integrity)):
                counts[kind] = counts.get(kind, 0) + count
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"kind": kind, "count": count} for kind, count in ordered]

    def neighbourhood(self, node_id: str, hops: int = MAX_HOPS, limit: int = MAX_NODES) -> dict:
        """The visible nodes and edges within ``hops`` steps of the node, at most ``limit`` nodes. Raises
        NotVisible."""
        if self.error:
            raise NotVisible(node_id)
        hops = max(1, min(int(hops), MAX_HOPS))
        limit = max(1, min(int(limit), MAX_NODES))
        store, b = self._visible_store()
        focus = self._lookup(store, node_id, b)
        if focus is None:
            raise NotVisible(node_id)
        self.read = None
        included: dict[str, dict] = {focus["id"]: focus}
        hop: dict[str, int] = {focus["id"]: 0}
        hubs: set[str] = set()
        edges: dict[str, dict] = {}
        left_out: set[str] = set()
        neighbours: set[str] = set()
        frontier = deque([(focus["id"], 0)])
        while frontier:
            current, depth = frontier.popleft()
            if depth >= hops:
                continue
            found = [e for e in store.edges_of(current, b, limit=SEED_EDGES) if self.visible(e["label"])]
            if depth > 0 and len(found) > HUB_DEGREE:
                hubs.add(current)  # not expanded: open it to see its neighbours
                continue
            found.sort(
                key=lambda e: (
                    -STATUS_RANK.get(e["status"], 0),
                    e["rel"] in ("in_source", "part_of"),
                    e["rel"],
                    e["src"],
                    e["dst"],
                )
            )
            others = store.nodes({e["dst"] if e["src"] == current else e["src"] for e in found}, b)
            for e in found:
                other = e["dst"] if e["src"] == current else e["src"]
                node = others.get(other)
                if node is None or not self.visible(node["label"]):
                    continue
                if depth == 0:
                    neighbours.add(other)
                if other not in included:
                    if len(included) >= limit:
                        left_out.add(other)
                        continue
                    included[other] = node
                    hop[other] = depth + 1
                    frontier.append((other, depth + 1))
                edges[e["id"]] = e
        nodes = []
        for nid, n in included.items():
            entry = self._node_json(n)
            entry["hop"] = hop[nid]
            entry["hub"] = nid in hubs
            nodes.append(entry)
        shown = []
        for e in edges.values():
            if e["src"] in included and e["dst"] in included:
                self._saw(e["label"])
                shown.append({"id": e["id"], "src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]})
        return {
            "build": b,
            "focus": focus["id"],
            "hops": hops,
            "limit": limit,
            "nodes": nodes,
            "edges": shown,
            "neighbours": len(neighbours),
            "left_out": len(left_out),
            "truncated": bool(left_out),
        }


def read(
    cache: GraphCache,
    store: BlobStore | None,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    graphs: list[BuiltGraph],
    reader: Callable[[WebSession], T],
) -> tuple[T, BuiltGraph]:
    """``reader`` over the newest graph of ``project`` that can be read, and that graph. Raises
    GraphUnavailable, ArtifactMismatch (``evo_agents.hub.kg_graph``), NotVisible, GraphProblem."""
    path, graph = cache.acquire(store, project, graphs)
    try:
        opened = Store.open_file(path, immutable=True)
        try:
            return reader(WebSession(project, policy, ceiling, WEB_SINK, opened, graph, graphs[0])), graph
        finally:
            opened.close()
    finally:
        cache.release(path)
