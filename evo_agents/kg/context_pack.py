"""The budgeted subgraph of kg_context, chosen by a monotone submodular objective instead of a flat walk.

The objective of a set S of nodes is ``f(S) = sum(d(v) * relevance(v)) + COVERAGE_WEIGHT * sum_f w_f * (1 - DECAY **
c_f(S))`` where ``c_f(S)`` is the discounted number of nodes of S that touch facet ``f`` (a concept of the question, or
the neighbourhood of a seed). A sum of a modular term and concave functions of modular terms is monotone and
submodular, so the greedy that picks, while the budget lasts, the node with the best ``marginal_gain(v | S) /
cost(v)`` and then compares its set with the best single node keeps (1 - 1/e) / 2 of the best set the budget allows
(Leskovec and others, cost-effective lazy greedy; Khuller, Moss and Naor for the knapsack constraint). ``d(v)`` is the
session discount: a node an earlier kg_context of the session already gave is worth ``REPEAT_DISCOUNT`` per time.

``bridge`` joins seeds by shortest paths, a prize-collecting Steiner tree approximation: entering a node ``u`` costs
``ln(2 + deg(u))``, so a path goes through a node of moderate degree when it must but a star's centre only when no
other way joins the seeds. It replaces the hard cut at HUB_DEGREE: nothing is skipped, hubs are dear.

This module holds numbers and ids only; ``evo_agents.kg.serve_pack`` reads the graph for it.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

REPEAT_DISCOUNT = 0.2  # what a node already given earlier in the session is worth, per time it was given
COVERAGE_WEIGHT = 0.5  # the facet term next to the relevance of a node
DECAY = 0.5  # a facet touched c times is covered to 1 - DECAY ** c
MAX_EXPANDED = 200  # nodes a bridge search reads the neighbours of


@dataclass(frozen=True)
class Item:
    """A candidate: its token cost (bytes of its lines), its relevance to the question (seed relevance or the walk's
    score, 0 to 1) and the facets it touches."""

    id: str
    cost: int
    relevance: float
    facets: frozenset[str] = frozenset()


def repeat_discount(times: int) -> float:
    """What a node given ``times`` times before is worth now: 1 for a new one, REPEAT_DISCOUNT ** times."""
    return REPEAT_DISCOUNT ** max(int(times), 0)


class Objective:
    """f(S) as nodes join S: ``gain`` is the marginal gain of one more, ``add`` puts it in."""

    def __init__(self, given: Mapping[str, int] | None = None, facet_weights: Mapping[str, float] | None = None):
        self.given = given or {}
        self.weights = facet_weights or {}
        self.covered: Counter = Counter()  # facet: discounted number of nodes of S that touch it

    def discount(self, item: Item) -> float:
        return repeat_discount(self.given.get(item.id, 0))

    def gain(self, item: Item) -> float:
        d = self.discount(item)
        total = d * item.relevance
        for facet in item.facets:
            before = self.covered[facet]
            total += COVERAGE_WEIGHT * self.weights.get(facet, 1.0) * (DECAY**before - DECAY ** (before + d))
        return total

    def add(self, item: Item) -> float:
        got = self.gain(item)
        d = self.discount(item)
        for facet in item.facets:
            self.covered[facet] += d
        return got


def pack_subgraph(
    items: Iterable[Item],
    budget: int,
    *,
    given: Mapping[str, int] | None = None,
    facet_weights: Mapping[str, float] | None = None,
    forced: Iterable[Item] = (),
) -> list[str]:
    """The ids, in the order chosen, of the items whose costs fit ``budget`` and whose objective is highest by the
    cost-effective greedy: the best ``gain / cost`` first, a stale gain refreshed lazily (it can only have fallen),
    then the better of that set and the best single item that fits. ``forced`` items are already in S (the seeds):
    they shape the gains and cost nothing here."""
    pool = {item.id: item for item in items}
    base = Objective(given, facet_weights)
    for item in forced:
        base.add(item)
        pool.pop(item.id, None)
    single = _best_single(pool.values(), base, budget)
    chosen, value = _greedy(pool, base, budget)
    if single is not None and base.gain(single) > value:
        return [single.id]
    return chosen


def _best_single(items: Iterable[Item], objective: Objective, budget: int) -> Item | None:
    fits = [item for item in items if 0 < item.cost <= budget]
    return max(fits, key=lambda item: (objective.gain(item), item.id), default=None)


def _greedy(pool: Mapping[str, Item], objective: Objective, budget: int) -> tuple[list[str], float]:
    heap = [(-objective.gain(item) / max(item.cost, 1), item.id) for item in pool.values() if item.cost <= budget]
    heapq.heapify(heap)
    chosen: list[str] = []
    value, left = 0.0, budget
    while heap:
        _, nid = heapq.heappop(heap)
        item = pool[nid]
        if item.cost > left:
            continue
        fresh = objective.gain(item) / max(item.cost, 1)
        if heap and fresh < -heap[0][0]:
            heapq.heappush(heap, (-fresh, nid))  # its gain fell below another's: look at the other first
            continue
        if fresh <= 0:
            break
        value += objective.add(item)
        chosen.append(nid)
        left -= item.cost
    return chosen, value


def step_cost(degree: int) -> float:
    """What a path pays to enter a node of ``degree`` edges: ln(2 + degree), the soft hub penalty."""
    return math.log(2 + max(degree, 0))


def bridge(
    seeds: list[str],
    neighbours: Callable[[str], Iterable[tuple[str, str]]],
    degree: Callable[[str], int],
    max_cost: float,
    max_expanded: int = MAX_EXPANDED,
) -> tuple[list[str], list[str]]:
    """(nodes, edge ids) of a tree that joins the seeds it can through paths of at most ``max_cost`` each, grown from
    the first seed: the seed nearest the tree joins by its shortest path (Dijkstra, ``step_cost`` per node entered).
    A seed no path of ``max_cost`` reaches stays out (the prize is not worth it). ``neighbours(u)`` gives
    ``(other node, edge id)``. The nodes exclude the seeds."""
    if len(seeds) < 2:
        return [], []
    tree, rest, given = {seeds[0]}, set(seeds[1:]), set(seeds)
    nodes: list[str] = []
    edges: list[str] = []
    while rest:
        found = _nearest(tree, rest, neighbours, degree, max_cost, max_expanded)
        if found is None:
            break
        path, via = found
        for nid, eid in zip(path, via, strict=True):
            if nid not in tree and nid not in given:
                nodes.append(nid)
            tree.add(nid)
            edges.append(eid)
        rest.discard(path[-1])
    return nodes, edges


def _nearest(tree, rest, neighbours, degree, max_cost: float, max_expanded: int) -> tuple[list[str], list[str]] | None:
    """The cheapest path from ``tree`` to a node of ``rest``: (nodes after the tree, the edge ids into each), or None."""
    best: dict[str, float] = {nid: 0.0 for nid in tree}
    back: dict[str, tuple[str, str]] = {}
    heap = [(0.0, nid) for nid in sorted(tree)]
    heapq.heapify(heap)
    expanded = 0
    while heap and expanded < max_expanded:
        cost, nid = heapq.heappop(heap)
        if cost > best.get(nid, math.inf):
            continue
        if nid in rest:
            return _unwind(nid, back, tree)
        expanded += 1
        for other, eid in neighbours(nid):
            reach = cost + step_cost(degree(other))
            if reach <= max_cost and reach < best.get(other, math.inf):
                best[other] = reach
                back[other] = (nid, eid)
                heapq.heappush(heap, (reach, other))
    return None


def _unwind(end: str, back: Mapping[str, tuple[str, str]], tree) -> tuple[list[str], list[str]]:
    path, via = [], []
    nid = end
    while nid not in tree:
        path.append(nid)
        parent, eid = back[nid]
        via.append(eid)
        nid = parent
    return path[::-1], via[::-1]
