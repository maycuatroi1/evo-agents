"""Personalized PageRank around a question's matches, after HippoRAG 2 (Gutiérrez et al., arXiv 2502.14802).

HippoRAG 2 seeds a random walk with restart at the nodes a query matches and ranks passages by where the walk settles,
so a node that many good matches reach over a few hops rises even when its own words miss the query. ``locate`` already
lends a share of the best nodes' scores to their direct neighbours; ``walk`` goes further: it reads the graph up to
``hops`` hops from the seeds (a hub node lends to at most ``fanout`` of its edges), weighs each edge by its relation and
its confidence, and runs the walk on that subgraph, both ways along every edge as HippoRAG's graph is undirected.
Nothing here calls a model: the seeds are the text matches and the nodes the question names, not facts an LLM read.

Experimental: off unless $EVO_KG_PPR is set (``from_env``), so the shipped order does not change.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace

ENV = "EVO_KG_PPR"
CONF_FLOOR = 0.5  # a resolved edge below this confidence carries no walk


@dataclass(frozen=True)
class Walk:
    mix: float = 1.0  # the walk's score, scaled to the best base score, added to locate's score with this weight
    alone: bool = False  # rank by the walk alone (HippoRAG's order), anchors kept
    docs: int = 0  # a pool of mostly documents, which locate leaves in the text order: 1 locate's order, 2 by node
    hops: int = 2
    seeds: int = 40  # the candidates, best base first, the walk restarts at
    widen: int = 80  # the nodes of a hop, by the mass that reached them, read for the next hop
    fanout: int = 60  # the edges of one node read; a hub's others are left out
    max_nodes: int = 3000
    damping: float = 0.5  # HippoRAG 2's: the walk restarts half the time
    steps: int = 30
    keep: int = 60  # the nodes the walk reached outside the pool that join it as candidates
    context: bool = False  # kg_context fills its budget past the seeds by the walk, not by hops from them
    file_seed: float = 1.0  # a File seed's share of its base score (HippoRAG 2 seeds passages at 0.05 of entities)
    specific: bool = True  # a seed weighs 1 / log2(2 + its degree) more, as HippoRAG divides by the passages of a node
    rels: tuple = (
        ("calls", 0.6),
        ("imports", 1.0),
        ("inherits", 0.5),
        ("overrides", 0.8),
        ("changes", 1.0),
        ("mentions", 0.9),
        ("defines", 0.8),
        ("member_of", 0.6),
        ("touches", 1.0),
        ("implements", 0.8),
        ("has_step", 0.5),
        ("verifies", 0.6),
        ("merged_as", 1.0),
        ("defined_in", 0.8),
        ("depends_on", 0.5),
        ("child_of", 0.4),
        ("part_of", 0.4),
        ("in_source", 0.3),
    )


FIELDS = {
    "mix": float,
    "alone": lambda v: v not in ("0", "", "off", "false"),
    "docs": int,
    "hops": int,
    "seeds": int,
    "widen": int,
    "fanout": int,
    "max_nodes": int,
    "damping": float,
    "steps": int,
    "keep": int,
    "file_seed": float,
    "context": lambda v: v not in ("0", "", "off", "false"),
    "specific": lambda v: v not in ("0", "", "off", "false"),
}


def from_env(environ=None) -> Walk | None:
    """The walk $EVO_KG_PPR asks for (``on``, or ``key=value`` pairs of ``Walk`` split by commas), None when unset."""
    raw = (environ if environ is not None else os.environ).get(ENV, "").strip()
    if not raw or raw in ("0", "off"):
        return None
    walk = Walk()
    for part in raw.split(","):
        key, _, value = part.partition("=")
        key = key.strip()
        if key in FIELDS and value:
            walk = replace(walk, **{key: FIELDS[key](value.strip())})
    return walk


def _edges(store, b: int, ids: list[str], rels: dict[str, float]) -> list[tuple[str, str, str, float]]:
    """(src, rel, dst, weight) of the live edges of ``ids`` either way, with one of ``rels``, the reader may see."""
    from evo_agents.kg.store import LIVE, SEEN

    marks_r = ", ".join(f":r{i}" for i in range(len(rels)))
    params: dict = {"b": b, **{f"r{i}": r for i, r in enumerate(rels)}}
    out = []
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ", ".join(f":n{i}" for i in range(len(chunk)))
        params_c = {**params, **{f"n{i}": n for i, n in enumerate(chunk)}}
        for side in ("src", "dst"):
            rows = store.db.execute(
                f"SELECT src, rel, dst, status, conf FROM edges WHERE {side} IN ({marks}) AND rel IN ({marks_r})"
                f" AND {LIVE} AND {SEEN}",
                params_c,
            )
            for src, rel, dst, status, conf in rows:
                c = 1.0 if status == "parsed" else float(conf or 0.0)
                if c >= CONF_FLOOR and src != dst:
                    out.append((src, rel, dst, rels[rel] * c))
    return out


def subgraph(store, b: int, seeds: dict[str, float], walk: Walk) -> dict[str, dict[str, float]]:
    """The weighted adjacency, both ways, of the nodes within ``walk.hops`` of ``seeds``."""
    rels = dict(walk.rels)
    adj: dict[str, dict[str, float]] = {s: {} for s in seeds}
    mass = dict(seeds)
    frontier = sorted(seeds, key=lambda i: (-seeds[i], i))[: walk.widen]
    read: set[str] = set()
    for _ in range(walk.hops):
        frontier = [i for i in frontier if i not in read]
        if not frontier:
            break
        read.update(frontier)
        by_node: dict[str, list[tuple[str, float]]] = {}
        for src, _rel, dst, w in _edges(store, b, frontier, rels):
            for here, there in ((src, dst), (dst, src)):
                if here in read and here in frontier:
                    by_node.setdefault(here, []).append((there, w))
        reached: dict[str, float] = {}
        for here in sorted(by_node):
            links = sorted(by_node[here], key=lambda t: (-t[1], t[0]))[: walk.fanout]
            for there, w in links:
                if there not in adj and len(adj) >= walk.max_nodes:
                    continue
                adj.setdefault(here, {})
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
    restart = {i: w / total for i, w in seeds.items()}
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


def run(store, b: int, base: dict[str, float], walk: Walk) -> dict[str, float]:
    """The walk's score of each node it reached from the best ``walk.seeds`` of ``base`` (id -> base score)."""
    best = sorted((i for i in base if base[i] > 0), key=lambda i: (-base[i], i))[: walk.seeds]
    seeds = {i: base[i] for i in best}
    if not seeds:
        return {}
    adj = subgraph(store, b, seeds, walk)
    if walk.specific:
        seeds = {i: w / math.log2(2 + len(adj.get(i, ()))) for i, w in seeds.items()}
    return personalized(adj, seeds, walk)
