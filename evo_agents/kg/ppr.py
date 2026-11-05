"""Personalized PageRank around a question's matches, after HippoRAG 2 (Gutiérrez et al., arXiv 2502.14802).

HippoRAG 2 seeds a random walk with restart at the entities a query matches and at the passages it matches, and ranks
the passages by where the walk settles: a passage that many matching entities reach over a few hops rises even when its
own words miss the query. A project turns the walk on with the ``walk`` key of its KG settings
(``evo_agents.kg.settings``); without it nothing here runs and kg_search and kg_context rank as they always have.

The walk, as ``Walk`` sets it:

- seeds. An entity node (one the ``entities`` build step stated, ``entity:<kind>:<name>``) among the text matches
  restarts the walk by its text match divided by the number of documents that mention it, as HippoRAG divides an
  entity's weight by its passages; any other text match restarts it by ``passage_seed`` (0.05, HippoRAG 2's
  ``passage_node_weight``) times its text match. A graph whose matches hold no entity (no ``entities`` step ran) seeds
  at the text matches alone, the best weighed most and a node of many edges less (``fallback_seeds``).
- subgraph. The edges are read up to ``hops`` hops from the seeds, both ways along each, at most ``fanout`` per node
  and ``max_nodes`` nodes in all. An edge weighs its relation (``REL_WEIGHTS``) times its status (``STATUS_WEIGHTS``)
  times its confidence when it was not read from a source; one under ``conf_floor`` carries nothing.
- visibility. The walk never enters a node or an edge its reader does not see: ``seen`` (the serving session's, which
  compares level, location and audience with the reader's clearance, and leaves deprecated elements out) is asked of
  every seed, edge and node before it joins the subgraph, so no score flows through a hidden element.
- the walk itself. ``damping`` (0.5, HippoRAG 2's) is the share of the mass that follows an edge at each step; the
  rest restarts at the seeds. ``steps`` iterations.

Nothing here calls a model, reads a question set, or needs a library outside the standard one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, replace

ENTITY_PREFIX = "entity:"  # the ids of the nodes the entities step states (evo_agents.kg.entities.entity_id)
DEFAULT_REL = 1.0  # a relation the table does not list: one a project's ontology adds or a model stated
REL_WEIGHTS = {
    "calls": 0.6,
    "imports": 1.0,
    "inherits": 0.5,
    "overrides": 0.8,
    "changes": 1.0,
    "mentions": 1.0,
    "defines": 0.8,
    "declares": 0.8,
    "member_of": 0.6,
    "touches": 1.0,
    "implements": 0.8,
    "has_step": 0.5,
    "verifies": 0.6,
    "schedules": 0.6,
    "merged_as": 1.0,
    "defined_in": 0.8,
    "depends_on": 0.5,
    "child_of": 0.4,
    "part_of": 0.4,
    "in_source": 0.0,  # every node of a source shares it: a hub that says nothing of the question
    "in_repo": 0.0,  # the same of a repo
}
STATUS_WEIGHTS = {
    "declared": 1.0,
    "parsed": 1.0,
    "introspected": 1.0,
    "resolved": 1.0,
    "extracted": 1.0,
    "proposed": 0.0,  # nobody stated it: never walked
}
SURE = frozenset({"declared", "parsed", "introspected"})  # read from a source: their confidence is 1


class WalkError(ValueError):
    pass


@dataclass(frozen=True)
class Walk:
    damping: float = 0.5  # HippoRAG 2's: the share of the mass that follows an edge at each step
    passage_seed: float = 0.05  # HippoRAG 2's passage_node_weight: a matched non-entity node's share of a seed
    hops: int = 2  # edges read away from the seeds
    max_nodes: int = 3000  # nodes of the subgraph in all
    fanout: int = 60  # edges of one node read, the heaviest first; a hub's others are left out
    widen: int = 80  # nodes of a hop, by the mass that reached them, read for the next hop
    seeds: int = 40  # text matches, best first, the walk may restart at
    steps: int = 30  # iterations of the walk
    keep: int = 60  # nodes the walk reached beyond the text matches that kg_search lists
    conf_floor: float = 0.5  # an edge of a lower confidence carries no walk
    context: bool = True  # kg_context fills its budget past its seeds by the walk, not by hops from them


_RANGES = {
    "damping": (0.05, 0.95),
    "passage_seed": (0.0, 1.0),
    "hops": (1, 4),
    "max_nodes": (10, 20_000),
    "fanout": (1, 500),
    "widen": (1, 1000),
    "seeds": (1, 200),
    "steps": (1, 200),
    "keep": (0, 500),
    "conf_floor": (0.0, 1.0),
}
KEYS = tuple(f.name for f in fields(Walk))


def check(value) -> dict | None:
    """``value`` as the ``walk`` setting holds it: None for off (None, false, "off"), else every key of ``Walk`` with
    its value, sorted, so two settings that walk alike are equal (``true``, "on" and {} are the defaults); WalkError
    saying what is wrong."""
    if value is None or value is False or value in ("", "off"):
        return None
    if value is True or value == "on":
        value = {}
    if not isinstance(value, dict):
        raise WalkError('walk must be an object of Walk keys, true or "on" for the defaults, or null for off')
    walk = Walk()
    for key, raw in value.items():
        if key not in KEYS:
            raise WalkError(f"walk.{key} is not a key of the walk: {', '.join(KEYS)}")
        if key == "context":
            if not isinstance(raw, bool):
                raise WalkError("walk.context must be true or false")
            walk = replace(walk, context=raw)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise WalkError(f"walk.{key} must be a number")
        low, high = _RANGES[key]
        if not low <= raw <= high:
            raise WalkError(f"walk.{key} {raw} is outside {low:g} to {high:g}")
        if isinstance(low, int) and isinstance(high, int):
            if raw != int(raw):
                raise WalkError(f"walk.{key} {raw} must be a whole number")
            raw = int(raw)
        else:
            raw = float(raw)
        walk = replace(walk, **{key: raw})
    return {k: getattr(walk, k) for k in sorted(KEYS)}


def of(value) -> Walk | None:
    """The ``Walk`` of a ``walk`` setting (as ``check`` holds it), None when off."""
    held = check(value)
    return None if held is None else Walk(**held)


def is_entity(node_id: str) -> bool:
    return node_id.startswith(ENTITY_PREFIX)


# -- the subgraph ----------------------------------------------------------------------------------------------------


class Reader:
    """What a reader sees of build ``b``: ``seen`` of each node and edge, nodes read once."""

    def __init__(self, store, b: int, seen):
        self.store, self.b, self.seen = store, b, seen
        self.nodes: dict[str, bool] = {}

    def visible(self, ids) -> set[str]:
        """The ids of ``ids`` whose node the reader sees (live at b, its audience within the store's reader, and
        ``seen``)."""
        fresh = [i for i in dict.fromkeys(ids) if i not in self.nodes]
        if fresh:
            found = self.store.nodes(fresh, self.b)
            for i in fresh:
                node = found.get(i)
                self.nodes[i] = node is not None and bool(self.seen(node))
        return {i for i in ids if self.nodes.get(i)}

    def edges(self, ids: list[str], walk: Walk) -> list[tuple[str, str, str, float]]:
        """(src, rel, dst, weight) of the live edges of ``ids``, either way, the reader sees and the walk follows."""
        from evo_agents.kg.store_filters import LIVE, SEEN, stored_audience

        columns = "src, rel, dst, props, status, conf, label_lvl, label_loc, label_int, label_aud"
        out = []
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            marks = ", ".join(f":n{i}" for i in range(len(chunk)))
            params = {"b": self.b, **{f"n{i}": n for i, n in enumerate(chunk)}}
            for side in ("src", "dst"):
                rows = self.store.db.execute(
                    f"SELECT {columns} FROM edges WHERE {side} IN ({marks}) AND {LIVE} AND {SEEN}", params
                )
                for src, rel, dst, props, status, conf, lvl, loc, integrity, aud in rows:
                    if src == dst:
                        continue
                    e = {
                        "src": src,
                        "rel": rel,
                        "dst": dst,
                        "props": json.loads(props or "{}"),
                        "status": status,
                        "conf": conf,
                        "label": (lvl, loc, integrity, stored_audience(aud)),
                    }
                    w = weight(e, walk)
                    if w > 0 and self.seen(e):
                        out.append((e["src"], e["rel"], e["dst"], w))
        return out


def weight(e: dict, walk: Walk) -> float:
    """An edge's weight in the walk: its relation's, times its status's, times its confidence unless it was read from
    a source; 0 under ``walk.conf_floor``."""
    status = e.get("status")
    conf = 1.0 if status in SURE else float(e.get("conf") or 0.0)
    if conf < walk.conf_floor:
        return 0.0
    return REL_WEIGHTS.get(e["rel"], DEFAULT_REL) * STATUS_WEIGHTS.get(status, 0.0) * conf


def subgraph(reader: Reader, seeds: dict[str, float], walk: Walk) -> dict[str, dict[str, float]]:
    """The weighted adjacency, both ways, of the visible nodes within ``walk.hops`` of ``seeds``."""
    adj: dict[str, dict[str, float]] = {s: {} for s in seeds}
    mass = dict(seeds)
    frontier = sorted(seeds, key=lambda i: (-seeds[i], i))[: walk.widen]
    read: set[str] = set()
    for _ in range(walk.hops):
        frontier = [i for i in frontier if i not in read]
        if not frontier:
            break
        read.update(frontier)
        here_set = set(frontier)
        by_node: dict[str, dict[str, float]] = {}
        for src, _rel, dst, w in reader.edges(frontier, walk):
            for here, there in ((src, dst), (dst, src)):
                if here in here_set:
                    links = by_node.setdefault(here, {})
                    links[there] = max(links.get(there, 0.0), w)
        shown = reader.visible({t for links in by_node.values() for t in links})
        reached: dict[str, float] = {}
        for here in sorted(by_node):
            links = sorted(((t, w) for t, w in by_node[here].items() if t in shown), key=lambda t: (-t[1], t[0]))
            links = links[: walk.fanout]
            for there, w in links:
                if there not in adj and len(adj) >= walk.max_nodes:
                    continue
                adj.setdefault(there, {})
                adj[here][there] = max(adj[here].get(there, 0.0), w)
                adj[there][here] = max(adj[there].get(here, 0.0), w)
                reached[there] = reached.get(there, 0.0) + mass.get(here, 0.0) * w / len(links)
        for node, m in reached.items():
            mass[node] = mass.get(node, 0.0) + m
        frontier = sorted(reached, key=lambda i: (-reached[i], i))[: walk.widen]
    return adj


def personalized(adj: dict[str, dict[str, float]], seeds: dict[str, float], walk: Walk) -> dict[str, float]:
    """The walk's resting score of each node of ``adj``, restarting at ``seeds`` by their weight."""
    total = sum(seeds.values())
    if not adj or total <= 0:
        return {}
    restart = {i: w / total for i, w in seeds.items() if w > 0}
    out_w = {i: sum(links.values()) for i, links in adj.items()}
    rank = dict(restart)
    for _ in range(walk.steps):
        lone = sum(r for i, r in rank.items() if not out_w.get(i))  # a node of no edge gives back to the seeds
        nxt = {i: (1 - walk.damping + walk.damping * lone) * r for i, r in restart.items()}
        for i, r in rank.items():
            ow = out_w.get(i)
            if not ow or not r:
                continue
            share = walk.damping * r / ow
            for o, w in adj[i].items():
                nxt[o] = nxt.get(o, 0.0) + share * w
        rank = nxt
    return rank


# -- the seeds -------------------------------------------------------------------------------------------------------


def _rels(scored) -> dict[str, float]:
    """The text match of each id of ``scored`` (id, score or None) in 0..1, as ``locate`` reads it."""
    from evo_agents.kg import locate

    return locate._rels(scored)


def _documents_of(reader: Reader, entity: str) -> int:
    """How many visible nodes mention ``entity`` (an entity's passages, in HippoRAG's words)."""
    found = reader.store.edges_of(entity, reader.b, "in", limit=500)
    srcs = [e["src"] for e in found if e["rel"] == "mentions" and reader.seen(e)]
    return len(reader.visible(srcs))


def seed(reader: Reader, base: dict[str, float], walk: Walk) -> dict[str, float]:
    """The restart weights of a question's visible matches ``base`` (id -> text match in 0..1, best first): HippoRAG 2's
    seeds when an entity is among them, else ``fallback_seeds``."""
    best = sorted((i for i in base if base[i] > 0), key=lambda i: (-base[i], i))[: walk.seeds]
    best = [i for i in best if i in reader.visible(best)]
    entities = [i for i in best if is_entity(i)]
    if not entities:
        return fallback_seeds(reader, {i: base[i] for i in best}, walk)
    seeds = {i: base[i] / max(_documents_of(reader, i), 1) for i in entities}
    for i in best:
        if not is_entity(i) and walk.passage_seed > 0:
            seeds[i] = walk.passage_seed * base[i]
    return seeds


def fallback_seeds(reader: Reader, base: dict[str, float], walk: Walk) -> dict[str, float]:
    """The seeds of a graph whose matches hold no entity, as the experimental walk read them: each match by the cube
    of its text match (the best weigh most), divided by log2(2 + its edges in the subgraph)."""
    seeds = {i: s**3 for i, s in base.items()}
    if not seeds:
        return {}
    adj = subgraph(reader, seeds, replace(walk, hops=1))
    return {i: w / math.log2(2 + len(adj.get(i, ()))) for i, w in seeds.items()}


def run(store, b: int, base: dict[str, float], walk: Walk, seen) -> dict[str, float]:
    """The walk's score of each visible node it reached from ``base`` (id -> text match in 0..1); ``seen(element)`` is
    the reader's test of a node or an edge (``Session.seen``)."""
    reader = Reader(store, b, seen)
    seeds = seed(reader, base, walk)
    if not seeds:
        return {}
    return personalized(subgraph(reader, seeds, walk), seeds, walk)


def order(store, b: int, scored, walk: Walk, seen) -> list[str]:
    """kg_search's ids under the walk: the matches the search gave no score (an id or a name is the query) first, then
    the nodes the walk reached that are not entities, by its score, then the entities it reached, then the other text
    matches in the text order. ``scored`` is ``Store.search_scored``: (id, text score), best first."""
    scored = list(scored)
    found = run(store, b, _rels(scored), walk, seen)
    matched = {i for i, _ in scored}
    reached = sorted(found, key=lambda i: (-found[i], i))
    beyond = [i for i in reached if i not in matched][: walk.keep]
    keep = set(beyond) | matched
    listed = [i for i, s in scored if s is None]
    listed += [i for i in reached if i in keep and not is_entity(i)]
    listed += [i for i in reached if i in keep and is_entity(i)]
    listed += [i for i, _ in scored]
    return list(dict.fromkeys(listed))
