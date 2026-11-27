"""The subgraph of kg_context past its seeds: what ``evo_agents.kg.context_pack`` chooses from the nodes the seeds
reach. The reach is read best first with a soft penalty on hubs (a node's relevance passes on divided by ``ln(2 +
degree)``), the seeds are bridged through the cheapest paths, and the budget goes by marginal gain per byte, with the
nodes an earlier kg_context of the session gave discounted."""

from __future__ import annotations

import heapq
import math
from collections import Counter

from evo_agents.kg import context_pack, locate, ppr
from evo_agents.kg.asked import terms
from evo_agents.kg.budget import CONTEXT_MIN_LINE, Room, line_size
from evo_agents.kg.serve_tool import Tool
from evo_agents.kg.snapshot import SnapshotView

STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "extracted": 1.5, "proposed": 1}
SEED_EDGES = 500  # edges read around a seed, which is expanded even when it is a hub
EDGES = 300  # edges read around any other node
EXPANDED = 40  # nodes the reach reads the neighbours of
SOFT_RELS = ("in_source", "part_of")  # edges every item has: they carry little of the question
BRIDGE_COST = 12.0  # the most a path between two seeds may cost (``context_pack.step_cost`` per node entered)
GIVEN_KEPT = 2000  # nodes of a session the discount remembers


class Reach:
    """The nodes within ``hops`` of the seeds with the relevance and the edge each came by."""

    def __init__(self) -> None:
        self.relevance: dict[str, float] = {}
        self.parent: dict[str, dict] = {}
        self.origin: dict[str, set[str]] = {}
        self.nodes: dict[str, dict] = {}


class ContextPack(Tool):
    """The part of kg_context that fills the budget past the seeds (``evo_agents.kg.serve_walk.KgContext``)."""

    def _adjacent(self, store: SnapshotView, cache: dict, nid: str, limit: int) -> list[tuple[str, dict]]:
        """(other end, edge) of the visible edges of ``nid``, read once per call."""
        if nid not in cache:
            found = [e for e in store.edges_of(nid, limit=limit) if self.visibility.seen(e)]
            cache[nid] = [(e["dst"] if e["src"] == nid else e["src"], e) for e in found]
        return cache[nid]

    def _reach(self, store: SnapshotView, seeds: dict[str, dict], hops: int, cache: dict) -> Reach:
        """Best-first from the seeds: relevance 1 at a seed, and across an edge the relevance of its end times its
        status over ``ln(2 + degree)`` of the end it left, so a hub passes on little but is still read."""
        reach = Reach()
        heap = []
        for sid, node in seeds.items():
            reach.relevance[sid], reach.origin[sid], reach.nodes[sid] = 1.0, {sid}, node
            heap.append((-1.0, 0, sid))
        heapq.heapify(heap)
        done: set[str] = set()
        while heap and len(done) < EXPANDED:
            neg, depth, nid = heapq.heappop(heap)
            if nid in done or -neg < reach.relevance[nid] or depth >= hops:
                continue
            done.add(nid)
            damp = math.log(2 + store.degree(nid))
            links = self._adjacent(store, cache, nid, SEED_EDGES if nid in seeds else EDGES)
            known = store.nodes({o for o, _ in links if o not in reach.nodes})
            for other, e in links:
                node = reach.nodes.get(other) or known.get(other)
                if node is None or not self.visibility.seen(node):
                    continue
                share = STATUS_RANK.get(e["status"], 1) / 5 * (0.3 if e["rel"] in SOFT_RELS else 1.0)
                score = reach.relevance[nid] * share / damp
                if score > reach.relevance.get(other, 0.0) and other not in seeds:
                    reach.relevance[other], reach.parent[other], reach.nodes[other] = score, e, node
                    reach.origin[other] = set(reach.origin[nid])
                    heapq.heappush(heap, (-score, depth + 1, other))
        return reach

    def _walked(self, store: SnapshotView, query, room: Room, walk, reach: Reach) -> None:
        """Add the nodes the walk settles on (``evo_agents.kg.ppr``) to the reach, scored by its probability."""
        scored = (
            store.search_scored(str(query), limit=walk.seeds, deprecated=self.visibility.deprecated) if query else []
        )
        base = {sid: 1.0 for sid in room.nodes}
        for nid, rel in locate._rels(scored).items():
            base[nid] = max(base.get(nid, 0.0), rel)
        found = ppr.run(store, base, walk, self.visibility)
        order = sorted((i for i in found if i not in room.nodes), key=lambda i: (-found[i], i))[: walk.keep * 4]
        top = max((found[i] for i in order), default=0.0) or 1.0
        nodes = store.nodes([i for i in order if i not in reach.nodes])
        for nid in order:
            node = reach.nodes.get(nid) or nodes.get(nid)
            if node is not None and self.visibility.seen(node):
                reach.nodes[nid] = node
                reach.relevance[nid] = max(reach.relevance.get(nid, 0.0), 0.9 * found[nid] / top)

    def _items(self, reach: Reach, room: Room, query) -> tuple[list[context_pack.Item], dict[str, float]]:
        words = terms(str(query)) if query else []
        weights = {f"seed:{sid}": 1.0 for sid in room.nodes}
        weights.update({f"word:{w}": 1.0 for w in words})
        items = []
        for nid, node in reach.nodes.items():
            if nid in room.nodes:
                continue
            text = self.text.line(node).lower()
            facets = {f"seed:{s}" for s in reach.origin.get(nid, ())} | {f"word:{w}" for w in words if w in text}
            cost = line_size(self.text.line(node))
            if nid in reach.parent:
                cost += line_size(self.text.edge_line(reach.parent[nid]))
            items.append(context_pack.Item(nid, cost, reach.relevance.get(nid, 0.0), frozenset(facets)))
        return items, weights

    def _bridged(self, store: SnapshotView, room: Room, cache: dict, reach: Reach) -> dict[str, dict]:
        """Take the nodes that join the seeds into ``room``; the edges that did are returned by id."""
        seeds = list(room.nodes)

        def near(nid: str):
            return [
                (o, e["id"]) for o, e in self._adjacent(store, cache, nid, SEED_EDGES if nid in room.nodes else EDGES)
            ]

        degrees: dict[str, int] = {}

        def degree(nid: str) -> int:
            if nid not in degrees:
                degrees[nid] = store.degree(nid)
            return degrees[nid]

        nodes, edge_ids = context_pack.bridge(seeds, near, degree, BRIDGE_COST)
        found = store.nodes([n for n in nodes if n not in reach.nodes])
        for nid in nodes:
            node = reach.nodes.get(nid) or found.get(nid)
            if node is None or not self.visibility.seen(node):
                continue
            if not room.take_node(nid, node, line_size(self.text.line(node))):
                room.left_nodes.add(nid)
        by_id = {e["id"]: e for links in cache.values() for _, e in links}
        return {eid: by_id[eid] for eid in edge_ids if eid in by_id}

    def _context_pack(self, store: SnapshotView, query, room: Room, hops: int, walk, given: Counter) -> None:
        """Fill ``room`` past its seeds: bridge them, then the nodes of best marginal gain per byte."""
        cache: dict = {}
        walking = walk is not None and walk.context  # the walk's weights decide what is reached, not the hops
        reach = self._reach(store, dict(room.nodes), 0 if walking else hops, cache)
        if walking:
            self._walked(store, query, room, walk, reach)
        core = self._bridged(store, room, cache, reach)
        items, weights = self._items(reach, room, query)
        forced = [
            context_pack.Item(nid, 0, 1.0, frozenset(f"seed:{s}" for s in reach.origin.get(nid, ())))
            for nid in room.nodes
        ]
        chosen = context_pack.pack_subgraph(
            items, room.size - room.used, given=given, facet_weights=weights, forced=forced
        )
        for nid in chosen:
            node = reach.nodes[nid]
            if room.size - room.used < CONTEXT_MIN_LINE or not room.take_node(
                nid, node, line_size(self.text.line(node))
            ):
                room.left_nodes.add(nid)
            elif nid in reach.parent:
                core[reach.parent[nid]["id"]] = reach.parent[nid]
        room.left_nodes |= {i.id for i in items} - set(room.nodes)
        room.beyond = bool(room.left_nodes - set(room.nodes))
        self._pack_edges(room, cache, core)

    def _pack_edges(self, room: Room, cache: dict, core: dict[str, dict]) -> None:
        """The edges between the nodes of ``room``: those that joined it or bridged the seeds first, then the
        strongest."""
        every = {e["id"]: e for links in cache.values() for _, e in links}
        inside = [e for e in every.values() if e["src"] in room.nodes and e["dst"] in room.nodes]
        inside.sort(
            key=lambda e: (e["id"] not in core, -STATUS_RANK.get(e["status"], 0), e["rel"], e["src"], e["dst"], e["id"])
        )
        for e in inside:
            if e["id"] in room.edges:
                continue
            if room.take(line_size(self.text.edge_line(e))):
                room.edges[e["id"]] = e
            else:
                room.left_edges.add(e["id"])

    def _given_before(self, key: tuple) -> Counter:
        """The nodes given before the kg_context ``key`` asks: those of the calls before it, so the same call again
        (a retry) reads what it read the first time."""
        if self.session.last_context is not None and self.session.last_context[0] == key:
            return self.session.last_context[1]
        return self.session.given

    def _remember(self, room: Room, key: tuple, before: Counter) -> None:
        """The nodes this kg_context gave count against later ones of the session (``context_pack.repeat_discount``)."""
        self.session.last_context = (key, before)
        after = Counter(before)
        after.update(list(room.nodes))
        self.session.given = Counter(dict(after.most_common(GIVEN_KEPT))) if len(after) > GIVEN_KEPT else after
