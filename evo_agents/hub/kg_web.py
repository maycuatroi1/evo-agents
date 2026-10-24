"""What the hub's web pages read of a project's knowledge graph: node search, one node with its neighbours, the
neighbourhood the graph view draws, how many visible nodes of each kind the graph holds, its shape (visible edges by
source kind, edge type and target kind), the commit each source the member can see read, the cluster map with one
cluster and the compact whole graph (``evo_agents.hub.kg_map``), and the review queue (``evo_agents.hub.kg_review``).

Each read opens the project's latest successful build the way the kg_* tools do (``evo_agents.hub.kg_graph``: the
verified artifact cache, the fallback to a cached build when the blob store does not answer) and answers with the
code of those tools: ``tool_kg_search`` and ``tool_kg_node`` of ``evo_agents.kg.serve.Session`` as they are, and a
neighbourhood that walks like ``tool_kg_context``. Every node and edge goes through ``Session.visible``, the one
filter the tools use too; only the ceiling differs. The tools read with the meet of the member's grant and a sink's
clearance, the web with the grant alone (``ProjectRules.grant_label`` of ``evo_agents.hub.access``, the label
``visible_by_grant`` holds a memory's label below); both hold the member's audience. When that audience does not hold
every audience of the build, the build's nodes and edges are those it holds, and the sources with items that have an
audience name no commit (``HubSession.within``).

The neighbourhood is bounded: at most MAX_HOPS steps from the node shown and at most MAX_NODES nodes, the node
itself included. Like kg_context it reads visible edges strongest status first, keeps ``in_source`` and ``part_of``
for last, and does not expand a node past the first step when it has more than HUB_DEGREE visible edges (it would
fill the view with the neighbours of everything); unlike kg_context it stops at a node count instead of a character
budget and says what it left out. The node's direct neighbours fall in groups by edge type, direction and kind; the
node page asks for at most GROUP_SHOWN of each group, those with the most visible edges, and for the whole of a group
the reader opened, and the walk goes on from those alone. Results are the structured data of the tools, never their
text, so the 25,000 character cap of an MCP answer does not cut them.

Blocking: the api runs these in a thread, which also keeps the SQLite connection in the thread that made it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TypeVar

from evo_agents.hub import kg_decisions, kg_map, kg_review
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.kg_graph import BuiltGraph, GraphCache, HubSession, open_graph
from evo_agents.kg import freshness
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.serve import HUB_DEGREE, SEED_EDGES, STATUS_RANK, ToolError
from evo_agents.kg.store import Store

MAX_HOPS = 2
MAX_NODES = 150
SEARCH_LIMIT = 25  # kg_search's own maximum
GROUP_SHOWN = 5  # direct neighbours of each group the node page shows before the reader opens the group
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

    def sources(self) -> list[dict]:
        """The commit each git source of the build read (``evo_agents.kg.freshness.reads``), for the sources whose
        node the member can see."""
        if self.error:
            return []
        store, b = self._visible_store()
        coverage = store.build_row(b)["coverage"]
        if not self.whole(b):
            hidden = store.seen(b)["audience"]
            coverage = {"sources": [s for s in coverage.get("sources", []) if s["id"] not in hidden]}
        found = []
        for read in freshness.reads(coverage):
            node = store.node(f"source:{read['source']}", b)
            if node is not None and self.visible(node["label"]):
                found.append(read)
        return found

    def summary(self) -> dict:
        """What GET /v1/kg/{project}/graph shows: visible nodes by kind, and the commits sources read."""
        return {"kinds": self.kinds(), "sources": self.sources()}

    def shape(self) -> list[dict]:
        """The graph's shape: how many visible edges join each (source kind, edge type, target kind), counting an edge
        only when it and both its nodes are visible; most first."""
        if self.error:
            return []
        store, b = self._visible_store()
        counts: dict[tuple[str, str, str], int] = {}
        for src_kind, rel, dst_kind, edge, src, dst, count in store.shape_counts(b):
            if self.visible(edge) and self.visible(src) and self.visible(dst):
                key = (src_kind, rel, dst_kind)
                counts[key] = counts.get(key, 0) + count
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"src_kind": k[0], "rel": k[1], "dst_kind": k[2], "count": count} for k, count in ordered]

    def cluster_map(self, with_nodes: bool = True) -> dict:
        """The visible level-1 clusters, the links between them and (``with_nodes``) their visible members
        (``evo_agents.hub.kg_map``); empty with a reason when the build has none or none is visible."""
        if self.error:
            return kg_map.cluster_map(kg_map.View(reason=kg_map.NO_VISIBLE), with_nodes)
        store, b = self._visible_store()
        return kg_map.cluster_map(kg_map.view(store, b, self.visible), with_nodes)

    def cluster(self, cluster_id: str) -> dict:
        """One visible cluster with its subclusters, members and neighbours. Raises NotVisible."""
        if self.error:
            raise NotVisible(cluster_id)
        store, b = self._visible_store()
        found = kg_map.cluster_detail(kg_map.view(store, b, self.visible), cluster_id)
        if found is None:
            raise NotVisible(cluster_id)
        return found

    def whole_graph(self, limit: int = kg_map.WHOLE_NODES) -> dict:
        """Every visible node (at most ``limit``) and edge between them, compact, with each node's level-1 cluster."""
        if self.error:
            return {"nodes": [], "rels": [], "edges": [], "truncated": False}
        store, b = self._visible_store()
        return kg_map.whole(store, b, self.visible, limit)

    def review(self) -> dict:
        """The review queue as the member sees it (``evo_agents.hub.kg_review``): only items about nodes they can
        read, each cut to those nodes."""
        if self.error:
            return {"computed": False, "reason": kg_map.NO_VISIBLE, "items": []}
        store, b = self._visible_store()
        return kg_review.queue(store, b, self.visible, self._degrees)

    def decision_subjects(self, nodes, edges) -> dict:
        """Whether the build holds each node id and (src, rel, dst) edge decisions name, and whether the member sees
        it (``evo_agents.hub.kg_decisions.subjects``); all hidden for a member who sees nothing of the build."""
        if self.error:
            return {key: kg_decisions.HIDDEN for key in [*nodes, *edges]}
        store, b = self._visible_store()
        return kg_decisions.subjects(store, b, self.visible, nodes, edges)

    def _degrees(self, store: Store, ids, b: int) -> dict[str, int]:
        """Visible edges of each node of ``ids``, by the same ceiling ``visible`` compares labels with."""
        return store.degrees(ids, b, self.clearance)

    def neighbourhood(
        self,
        node_id: str,
        hops: int = MAX_HOPS,
        limit: int = MAX_NODES,
        group: int | None = None,
        opened: frozenset[str] = frozenset(),
    ) -> dict:
        """The visible nodes and edges within ``hops`` steps of the node, at most ``limit`` nodes. The node's direct
        neighbours fall in groups by edge type, direction and kind (``groups``); with ``group``, each group shows at
        most that many of them, highest visible degree first, unless its key is in ``opened``, and the walk goes on
        from those alone. Raises NotVisible."""
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
        groups: dict[str, dict] = {}
        degree: dict[str, int] = {}
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
            found = [e for e in found if _visible_node(self, others, _other(e, current))]
            if depth == 0:
                neighbours = {_other(e, current) for e in found}
                groups = _groups(found, current, others)
                degree.update(self._degrees(store, {nid for g in groups.values() for nid in g["members"]}, b))
                if group is not None:
                    rank = _ranked(groups, degree, group, opened)  # the direct neighbours shown, in order
                    found = [e for e in found if _other(e, current) in rank]
                    found.sort(key=lambda e: rank[_other(e, current)])  # stable: ties keep the order above
            for e in found:
                other = _other(e, current)
                node = others[other]
                if other not in included:
                    if len(included) >= limit:
                        left_out.add(other)
                        continue
                    included[other] = node
                    hop[other] = depth + 1
                    frontier.append((other, depth + 1))
                edges[e["id"]] = e
        missing = [nid for nid in included if nid not in degree]
        if missing:
            degree.update(self._degrees(store, missing, b))
        nodes = []
        for nid, n in included.items():
            entry = self._node_json(n)
            entry["hop"] = hop[nid]
            entry["hub"] = nid in hubs
            entry["degree"] = degree.get(nid, 0)
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
            "group": group,
            "nodes": nodes,
            "edges": shown,
            "groups": [
                {
                    "key": key,
                    "rel": g["rel"],
                    "direction": g["direction"],
                    "kind": g["kind"],
                    "total": len(g["members"]),
                    "shown": [nid for nid in _order(g["members"], degree) if nid in included],
                }
                for key, g in groups.items()
            ],
            "neighbours": len(neighbours),
            "left_out": len(left_out),
            "truncated": bool(left_out),
        }


def _other(edge: dict, current: str) -> str:
    return edge["dst"] if edge["src"] == current else edge["src"]


def _visible_node(session: WebSession, nodes: dict[str, dict], node_id: str) -> bool:
    node = nodes.get(node_id)
    return node is not None and session.visible(node["label"])


def group_key(direction: str, rel: str, kind: str) -> str:
    """The key of a group of direct neighbours, as the ``open`` parameter of the neighbourhood names it."""
    return f"{direction}:{rel}:{kind}"


def _groups(found: list[dict], current: str, others: dict[str, dict]) -> dict[str, dict]:
    """The direct neighbours of ``current`` by edge type, direction and kind, groups ordered by type, outgoing first,
    then kind; each group keeps its members' nodes."""
    groups: dict[str, dict] = {}
    for e in found:
        other = _other(e, current)
        direction = "out" if e["src"] == current else "in"
        kind = others[other]["kind"]
        key = group_key(direction, e["rel"], kind)
        entry = groups.setdefault(key, {"rel": e["rel"], "direction": direction, "kind": kind, "members": {}})
        entry["members"][other] = others[other]
    return dict(sorted(groups.items(), key=lambda kv: (kv[1]["rel"], kv[1]["direction"] != "out", kv[1]["kind"])))


def _order(members: dict[str, dict], degree: dict[str, int]) -> list[str]:
    """Members highest visible degree first, then by name and id."""
    return sorted(members, key=lambda nid: (-degree.get(nid, 0), members[nid]["name"] or nid, nid))


def _ranked(groups: dict[str, dict], degree: dict[str, int], group: int, opened: frozenset[str]) -> dict[str, int]:
    """The direct neighbours a grouped view shows, each with its place: group by group, the first ``group`` of each
    (all of an opened one), highest degree first. A neighbour of several groups takes its first place."""
    rank: dict[str, int] = {}
    for key, g in groups.items():
        ordered = _order(g["members"], degree)
        for nid in ordered if key in opened else ordered[:group]:
            rank.setdefault(nid, len(rank))
    return rank


def read(
    cache: GraphCache,
    store: BlobStore | None,
    project: str,
    policy: Policy,
    ceiling: Label | None,
    graphs: list[BuiltGraph],
    reader: Callable[[WebSession], T],
) -> tuple[T, BuiltGraph]:
    """``reader`` over the newest graph of ``project`` that can be read, and that graph, counting the nodes and edges
    the ceiling's audience holds. Raises GraphUnavailable, ArtifactMismatch (``evo_agents.hub.kg_graph``), NotVisible,
    GraphProblem."""
    path, graph = cache.acquire(store, project, graphs)
    try:
        opened = open_graph(path, project, graph)
        try:
            session = WebSession(project, policy, ceiling, WEB_SINK, opened, graph, graphs[0])
            return reader(session), session.counted(graph)
        finally:
            opened.close()
    finally:
        cache.release(path)
