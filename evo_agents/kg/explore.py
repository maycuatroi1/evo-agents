"""Exploration of kg_search's ranking: on a share RATE of the searches agents make on the hub, a candidate no agent read
after a search yet (no ``node_asked`` row, ``Store.asked_nodes``), drawn from the next POOL the ranking found past the
results, is put at rank 4 or 5, and the result's last line says which and with what chance (``MARK``). The facet that
reads the session (``evo_agents.hub.facets``) keeps that chance with the result in the search's label, so clicks on
such candidates measure how far down agents look whatever a node's relevance (``evo_agents.kg.asked.propensities``),
and a node the ranking keeps below the results gets a chance to be read and learned at all.

Only the hub's sessions of agents explore (``evo_agents.hub.server.mcp``): a local session, the eval, the tests and
the loop's lookups have no explorer, and search as the ranking says.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

RATE = 0.05  # the share of searches that explore
RANKS = (4, 5)  # where the candidate goes, either at even chance
POOL = 20  # the candidates past the results it is drawn from, at most
MARK = "explored: rank {rank}, p={p:.6g}, [{ref}]"
MARKED = re.compile(r"^explored: rank (\d+), p=([0-9.eE+-]+), \[([^\[\]\s]+)\]\s*$", re.M)


@dataclass
class Explorer:
    rate: float | None = None  # None: RATE, as it is when the search comes
    rng: random.Random = field(default_factory=random.Random)

    def pick(self, results: list, candidates: list) -> tuple[int, int, float] | None:
        """(index of the candidate drawn from ``candidates``, the rank it goes to, the chance it had to go there), or
        None when this search does not explore: the draw missed, there are fewer than RANKS[0] - 1 results to put it
        after, or no candidate."""
        rate = RATE if self.rate is None else self.rate
        if rate <= 0 or self.rng.random() >= rate:
            return None
        pool = candidates[:POOL]
        ranks = [r for r in RANKS if r - 1 <= len(results)]
        if not pool or not ranks:
            return None
        rank = self.rng.choice(ranks)
        index = self.rng.randrange(len(pool))
        return index, rank, rate / len(ranks) / len(pool)


def marked(text: str) -> tuple[int, float, str] | None:
    """(rank, chance, readable id) of the explored candidate a kg_search result names, or None."""
    found = MARKED.search(str(text or ""))
    if found is None:
        return None
    try:
        return int(found.group(1)), float(found.group(2)), found.group(3)
    except ValueError:
        return None
