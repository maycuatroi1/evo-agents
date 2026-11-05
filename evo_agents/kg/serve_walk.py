"""kg_context and kg_path of ``kg serve``: walks over the visible nodes and edges of a build, from seeds out to a
budget of text, or from one node along chains and to another."""

from __future__ import annotations

from collections import deque

from evo_agents.kg import locate, ppr
from evo_agents.kg.budget import (
    CONTEXT_LABEL_BYTES,
    CONTEXT_MIN_LINE,
    CONTEXT_NOTE_BYTES,
    CONTEXT_QUERY_SHOWN,
    Room,
    line_size,
)
from evo_agents.kg.refs import readable
from evo_agents.kg.serve_text import SessionText
from evo_agents.kg.serve_view import ToolError
from evo_agents.kg.store import Store

STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "extracted": 1.5, "proposed": 1}
HUB_DEGREE = 150
CHAIN_RELS = ("implements", "schedules", "verifies", "has_step", "touches")
PATH_SKIP = ("in_source",)  # every item hangs off its source: a path through one says nothing
SEED_EDGES = 500  # edges read around a seed or a path's start, which are expanded even when they are hubs


class WalkTools(SessionText):
    """kg_context and kg_path."""

    def tool_kg_context(
        self, query: str | None = None, ids: list | None = None, budget_tokens: int = 3000, hops: int = 2
    ) -> tuple[str, dict]:
        store, b = self._store()
        size = max(500, min(int(budget_tokens), 8000)) * 3
        hops = max(1, min(int(hops), 3))
        seeds = self._context_seeds(store, b, query, ids)
        if query:
            what = str(query)[:CONTEXT_QUERY_SHOWN]
        else:
            what = ", ".join(readable(s) for s in list(seeds)[:3]) + (f" and {len(seeds) - 3} more" * (len(seeds) > 3))

        def header(nodes: int, edges: int) -> str:
            return f"context for {what}: {nodes} nodes, {edges} edges (build {b}, connected through the seeds)"

        # The header with the widest counts, the label line, "edges:" and the note on what was left out come out of
        # the budget first; a node line is costed with its label, which the text drops when the label line has it.
        reserved = line_size(header(99_999, 99_999)) + CONTEXT_LABEL_BYTES + line_size("edges:") + CONTEXT_NOTE_BYTES
        room = Room(size - reserved)
        for sid in seeds:
            if not room.take_node(sid, seeds[sid], line_size(self._line(seeds[sid]))):
                room.left_nodes.add(sid)
        if not room.nodes:
            raise ToolError("budget_tokens holds none of the seeds: raise it, or pass fewer ids")
        frontier = deque((s, 0) for s in room.nodes)
        walk = ppr.of(store.tuning.walk)
        if walk is not None and walk.context:  # the walk fills the budget past the seeds, not hops from them
            self._context_walk(store, b, query, room, walk)
            frontier.clear()
        while frontier:
            if room.size - room.used < CONTEXT_MIN_LINE:
                room.beyond = any(depth < hops for _, depth in frontier)  # full: what lies past them is not read
                break
            current, depth = frontier.popleft()
            if depth >= hops or (depth > 0 and store.degree(current, b) > HUB_DEGREE):
                continue  # do not expand hubs: they would fill the budget with neighbours of everything
            self._context_from(store, b, current, depth, room, frontier)
        shared, label_line = self._labels(room.nodes.values())
        lines = [header(len(room.nodes), len(room.edges)), *label_line]
        lines += [self._line(n, shared) for n in room.nodes.values()]
        lines.append("edges:")
        for e in room.edges.values():
            self._saw(e["label"])
            lines.append(self._edge_line(e))
        lines += room.note()
        data = {
            "build": b,
            "seeds": list(seeds),
            "nodes": [self._node_json(n) for n in room.nodes.values()],
            "edges": [
                {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]} for e in room.edges.values()
            ],
            "left_out": {"nodes": room.left()[0], "edges": room.left()[1], "beyond": room.beyond},
            "summary": f"{len(room.nodes)} nodes",
        }
        return "\n".join(lines), data

    def _context_seeds(self, store: Store, b: int, query, ids) -> dict[str, dict]:
        """The visible seeds of kg_context: the nodes ``ids`` name (stored or readable ids), else the first five
        that match ``query``."""
        if ids:
            found = [self._lookup(store, i, b) for i in ids if isinstance(i, str)]
            seeds = {n["id"]: n for n in found if n is not None}
        elif query:
            walk = ppr.of(store.tuning.walk)
            if walk is not None:  # the first five of the walk's order (evo_agents.kg.ppr), as kg_search lists them
                scored = store.search_scored(query, b, limit=max(walk.seeds, 30), deprecated=self.deprecated)
                hits = ppr.order(store, b, scored, walk, self.seen)[:40]
            else:
                hits = store.search(query, b, limit=5, deprecated=self.deprecated)[:5]
            nodes = store.nodes(hits, b)
            seeds = dict([(h, nodes[h]) for h in hits if h in nodes and self.seen(nodes[h])][:5])
        else:
            raise ToolError("give a query or ids")
        if not seeds:
            raise ToolError("nothing visible matches; try kg_search")
        return seeds

    def _context_walk(self, store: Store, b: int, query, room: Room, walk) -> None:
        """Fill ``room`` past its seeds with the visible nodes a walk settles on most (``evo_agents.kg.ppr``), best
        first, each with up to two visible edges to the nodes already in. The walk restarts at the query's text matches
        and at the seeds."""
        scored = store.search_scored(str(query), b, limit=walk.seeds, deprecated=self.deprecated) if query else []
        base = locate._rels(scored) if scored else {}
        for sid in room.nodes:
            base[sid] = max(base.get(sid, 0.0), 1.0)
        found = ppr.run(store, b, base, walk, self.seen)
        order = [i for i in sorted(found, key=lambda i: (-found[i], i)) if i not in room.nodes][: walk.keep * 4]
        nodes = store.nodes(order, b)
        for nid in order:
            if room.size - room.used < CONTEXT_MIN_LINE:
                room.beyond = True
                break
            node = nodes.get(nid)
            if node is None or not self.seen(node):
                continue
            links = []
            for e in store.edges_of(nid, b, limit=300):
                other = e["dst"] if e["src"] == nid else e["src"]
                if other in room.nodes and self.seen(e) and e["id"] not in room.edges:
                    links.append(e)
            links.sort(key=lambda e: (-STATUS_RANK.get(e["status"], 0), e["rel"], e["src"], e["dst"]))
            links = links[:2]
            cost = line_size(self._line(node)) + sum(line_size(self._edge_line(e)) for e in links)
            if room.take_node(nid, node, cost):
                for e in links:
                    room.edges[e["id"]] = e
            else:
                room.left_nodes.add(nid)

    def _context_from(self, store: Store, b: int, current: str, depth: int, room: Room, frontier: deque) -> None:
        """Put the visible edges of ``current`` into ``room``, strongest first, each with the neighbour it brings
        when that one is new; what does not fit is counted as left out, and a new neighbour joins the frontier."""
        edges = [e for e in store.edges_of(current, b, limit=300) if self.seen(e)]
        edges.sort(
            key=lambda e: (
                -STATUS_RANK.get(e["status"], 0),
                e["rel"] in ("in_source", "part_of"),
                e["rel"],
                e["src"],
                e["dst"],
            )
        )
        neighbours = store.nodes({e["dst"] if e["src"] == current else e["src"] for e in edges}, b)
        for e in edges:
            other = e["dst"] if e["src"] == current else e["src"]
            node = neighbours.get(other)
            if node is None or not self.seen(node) or e["id"] in room.edges:
                continue
            cost = line_size(self._edge_line(e))
            if other in room.nodes:
                if room.take(cost):
                    room.edges[e["id"]] = e
                else:
                    room.left_edges.add(e["id"])
            elif room.take_node(other, node, cost + line_size(self._line(node))):
                room.edges[e["id"]] = e
                frontier.append((other, depth + 1))
            else:
                room.left_nodes.add(other)
                room.left_edges.add(e["id"])

    def _walk(
        self, store: Store, b: int, start: dict, max_hops: int, rels: tuple | None, goal: str | None
    ) -> dict[str, dict]:
        """Breadth-first over visible nodes and edges in both directions, as a tree of parents. A node keeps,
        among the parents one hop closer to the start, the one whose path has the highest total STATUS_RANK
        (ties: smaller parent id, then rel, then edge id), so the answer is deterministic. Hubs other than
        the start are reached but not expanded; a hidden node is never entered, so it breaks every path
        through it."""
        tree = {start["id"]: {"hop": 0, "score": 0, "parent": None, "edge": None, "node": start, "hub": False}}
        layer = [start["id"]]
        for hop in range(1, max_hops + 1):
            if goal in tree or not layer:
                break
            best: dict[str, tuple] = {}
            for nid in layer:
                edges = store.edges_of(nid, b, limit=SEED_EDGES if hop == 1 else HUB_DEGREE + 1)
                if hop > 1 and len(edges) > HUB_DEGREE:
                    tree[nid]["hub"] = True
                    continue
                for e in edges:
                    if e["rel"] in PATH_SKIP or (rels and e["rel"] not in rels) or not self.seen(e):
                        continue
                    other = e["dst"] if e["src"] == nid else e["src"]
                    if other in tree:
                        continue
                    key = (-(tree[nid]["score"] + STATUS_RANK.get(e["status"], 0)), nid, e["rel"], e["id"])
                    if other not in best or key < best[other][0]:
                        best[other] = (key, nid, e)
            found = store.nodes(best, b)
            layer = []
            for other in sorted(best):
                node = found.get(other)
                if node is None or not self.seen(node):
                    continue
                key, parent, e = best[other]
                tree[other] = {"hop": hop, "score": -key[0], "parent": parent, "edge": e, "node": node, "hub": False}
                layer.append(other)
        return tree

    def tool_kg_path(self, to: str | None = None, max_hops: int = 6, **kwargs) -> tuple[str, dict]:
        start_id = kwargs.pop("from", None)  # a Python keyword, so it cannot be a parameter name
        if kwargs:
            raise TypeError(f"unexpected argument(s) {', '.join(sorted(kwargs))}")
        if not isinstance(start_id, str) or not start_id:
            raise ToolError("give from (a node id), and to for a path between two nodes")
        store, b = self._store()
        max_hops = max(1, min(int(max_hops), 6))
        start = self._lookup(store, start_id, b)
        goal = self._lookup(store, to, b) if isinstance(to, str) and to else None
        if start is None or (to and goal is None):
            raise ToolError("no visible node has that id; search with kg_search")
        tree = self._walk(store, b, start, max_hops, None if goal else CHAIN_RELS, goal["id"] if goal else None)
        if goal is not None:
            return self._path_between(b, start, goal, tree, max_hops)
        children: dict[str, list[str]] = {}
        for nid, t in tree.items():
            if t["parent"] is not None:
                children.setdefault(t["parent"], []).append(nid)
        shared, label_line = self._labels(t["node"] for t in tree.values())
        lines = [
            f"chains from {readable(start['id'])} along {', '.join(CHAIN_RELS)} (build {b}): {len(tree) - 1} node(s)"
            f" within {max_hops} hop(s)",
            *label_line,
            self._line(start, shared),
        ]
        stack = sorted(children.get(start["id"], []), reverse=True)
        while stack:
            nid = stack.pop()
            t = tree[nid]
            lines.append(
                "  " * t["hop"]
                + f"{self._step(t['parent'], t['edge'])} {self._line(t['node'], shared)}"
                + (" (hub, not expanded)" if t["hub"] else "")
            )
            stack.extend(sorted(children.get(nid, []), reverse=True))
        data = {
            "build": b,
            "from": start["id"],
            "to": None,
            "nodes": [{**self._node_json(t["node"]), "hop": t["hop"], "hub": t["hub"]} for t in tree.values()],
            "edges": [_edge_json(t) for t in tree.values() if t["parent"] is not None],
            "summary": f"{len(tree) - 1} nodes",
        }
        return "\n".join(lines), data

    def _path_between(self, b: int, start: dict, goal: dict, tree: dict, max_hops: int) -> tuple[str, dict]:
        chain = []
        nid = goal["id"] if goal["id"] in tree else None
        while nid is not None:
            chain.append(tree[nid])
            nid = tree[nid]["parent"]
        chain.reverse()
        ends = f"{readable(start['id'])} to {readable(goal['id'])}"
        if not chain:
            lines = [
                f"no path within {max_hops} hop(s) from {ends} through visible nodes (build {b}; hubs are not expanded)"
            ]
        else:
            shared, label_line = self._labels(t["node"] for t in chain)
            lines = [f"path from {ends} (build {b}): {len(chain) - 1} hop(s)", *label_line, self._line(start, shared)]
            lines += [f"  {self._step(t['parent'], t['edge'])} {self._line(t['node'], shared)}" for t in chain[1:]]
        data = {
            "build": b,
            "from": start["id"],
            "to": goal["id"],
            "found": bool(chain),
            "hops": len(chain) - 1 if chain else None,
            "nodes": [self._node_json(t["node"]) for t in chain],
            "edges": [_edge_json(t) for t in chain[1:]],
            "summary": f"{len(chain) - 1} hops" if chain else "no path",
        }
        return "\n".join(lines), data


def _edge_json(t: dict) -> dict:
    e = t["edge"]
    return {"src": e["src"], "rel": e["rel"], "dst": e["dst"], "status": e["status"]}
