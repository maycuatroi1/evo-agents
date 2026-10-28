"""What agents asked before they read a node: the words of the kg_search queries whose results led to it, learned from
the hub's labels (``evo_agents.hub.kg_labels``) and written into the graph as the ``node_asked`` index
(``Store.write_asked``), which ``Store.search`` reads as one more column of the text a node matches.

A label is one kg_search and what the agent did next: the files of its results it read or edited (clicks, each with
the rank it had), and the files it opened after a grep that followed the search, which no result named (the search
missed them). Each such file is a node the query's words lead to, weighed:

- a click by the inverse of the propensity of its rank (``propensities``), the chance an agent looks that far down:
  a click at rank 1 says little about the node beyond its rank, one at rank 5 more. The propensity is the cascade
  model's, read from the labels themselves: the share of the searches showing rank k whose first click was not above
  k (an agent reads down until it clicks); with exploration labels enough (``explore``: a never-clicked candidate put
  at rank 4 or 5 at random), the ratio of rank 5 to rank 4 is that of the clicks on those candidates, which the cascade
  estimate cannot tell from relevance. Floored at PROPENSITY_FLOOR, so one click weighs at most 1 / PROPENSITY_FLOOR;
- a file opened after a grep by GREP_WEIGHT, more than any click: the search missed what the agent needed;
- each by its age, halved every HALF_LIFE_DAYS days.

A node keeps its MAX_WORDS heaviest words, each repeated by its weight up to MAX_REPEAT (``match`` reads the repeats as
term frequency), and a prior the search multiplies its score by, 1 to PRIOR_MAX by the node's whole weight, which
decays the same way. A row holds the labels of one sensitivity label (level, location, audience) only, and carries it:
a search reads only the rows its clearance and audience allow, so the words of a search reach only a sink its label
allows. A label whose query repeats a holdout question of the project's eval set (``holdout``) is left out, so the
eval does not learn its own answers.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from evo_agents.kg import refs
from evo_agents.kg import search as text_search

HALF_LIFE_DAYS = 30.0
PROPENSITY_FLOOR = 0.2  # a click weighs at most 1 / PROPENSITY_FLOOR = 5
GREP_WEIGHT = 6.0  # a file opened after a grep that followed the search: more than any click
MAX_WORDS = 24  # the words a node keeps, heaviest first
MAX_REPEAT = 8  # the most times a word is written in a node's row
PRIOR_MAX = 1.2  # the most a node's score is multiplied by for being clicked
PRIOR_SCALE = 3.0  # the weight at which the prior has gone 63% of the way to PRIOR_MAX
MIN_PROPENSITY_LABELS = 30  # labels with results the cascade estimate needs; below, 1 / rank
MIN_EXPLORED = 50  # explored results at each of ranks 4 and 5 the exploration ratio needs
EXPLORE_RANKS = (4, 5)
MAX_RANK = 25
HOLDOUT_OVERLAP = 0.8  # a query holding this share of a holdout question's words repeats it


def terms(text: str) -> list[str]:
    """The words of ``text`` as the asked index holds them and a query reads them: each word and camelCase part of
    two letters or more, lower case without accents, but the stop words, once each in order."""
    found = []
    for word in text_search.query_words(text):
        for term in (word, *text_search.camel_parts(word)):
            term = text_search.bare(term)
            if (len(term) >= 2 or term.isdigit()) and term not in text_search.BARE_STOP_WORDS:
                found.append(term)
    return list(dict.fromkeys(found))


def fts_query(text: str) -> str | None:
    """The FTS5 query of ``text`` against the asked index: any of its ``terms``; None when it has none."""
    found = terms(text)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in found) if found else None


def match(words: list[str], text: str) -> float:
    """How well a row's asked ``text`` matches a query's ``words`` (``terms``), from 0 to 1: each word of the query
    the row holds counts log(1 + its repeats), over what every word at MAX_REPEAT would. Not BM25: the index holds a
    few hundred short rows whose words repeat across them, where BM25's inverse document frequency reads near 0."""
    if not words:
        return 0.0
    counts: dict[str, int] = {}
    for term in text.split():
        counts[term] = counts.get(term, 0) + 1
    held = sum(math.log1p(min(counts[w], MAX_REPEAT)) for w in words if w in counts)
    return held / (len(words) * math.log1p(MAX_REPEAT))


def prior(weight: float) -> float:
    """The multiplier of a node clicked with total ``weight``: 1 for none, approaching PRIOR_MAX."""
    return 1.0 + (PRIOR_MAX - 1.0) * (1.0 - math.exp(-max(weight, 0.0) / PRIOR_SCALE))


def decay(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


@dataclass(frozen=True)
class Label:
    """One search's label as the build reads it: the query, its results and clicks (each a dict with ``id``,
    ``rank``, ``path``; an explored result also ``explored``, the rank it was put at, and ``p``, its chance), the files opened after its grep, when it was searched, and its
    sensitivity label as the graph's integers (level, location, audience as ``label_aud`` stores it, or None)."""

    query: str
    results: list[dict] = field(default_factory=list)
    clicks: list[dict] = field(default_factory=list)
    after_grep: list[str] = field(default_factory=list)
    when: datetime | None = None
    level: int = 0
    location: int = 0
    audience: str | None = None


@dataclass(frozen=True)
class Row:
    """One row of the asked index: a node, the sensitivity label of the labels it holds, its prior and its words."""

    node_id: str
    level: int
    location: int
    audience: str | None
    prior: float
    asked: str


def _rank(item: Mapping) -> int | None:
    rank = item.get("rank")
    return rank if isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= MAX_RANK else None


def _first_click(label: Label) -> int | None:
    ranks = [r for r in map(_rank, label.clicks) if r is not None]
    return min(ranks) if ranks else None


def _explored(label: Label) -> tuple[int, str] | None:
    """(rank, id) of the result exploration put in the label, if any."""
    for result in label.results:
        rank = _rank({"rank": result.get("explored")})  # the rank printed, which mentions can make the label's differ
        if rank in EXPLORE_RANKS and isinstance(result.get("p"), (int, float)) and isinstance(result.get("id"), str):
            return rank, result["id"]
    return None


def propensities(labels: Iterable[Label]) -> dict[int, float]:
    """The chance an agent looks at rank k of a search's results, k from 1 to MAX_RANK, non-increasing and at least
    PROPENSITY_FLOOR: the cascade estimate (the module's docstring), 1 / k while fewer than MIN_PROPENSITY_LABELS
    labels have results, and the exploration ratio for rank 5 once MIN_EXPLORED explorations came at ranks 4 and 5."""
    labels = [label for label in labels if label.results]
    shown, reached = defaultdict(int), defaultdict(int)
    explored, explored_clicks = defaultdict(int), defaultdict(int)
    for label in labels:
        n = max((r for r in map(_rank, label.results) if r is not None), default=0)
        first = _first_click(label)
        for k in range(1, n + 1):
            shown[k] += 1
            reached[k] += first is None or first >= k
        found = _explored(label)
        if found is not None:
            explored[found[0]] += 1
            explored_clicks[found[0]] += any(c.get("id") == found[1] for c in label.clicks)
    out, last = {}, 1.0
    for k in range(1, MAX_RANK + 1):
        if len(labels) < MIN_PROPENSITY_LABELS:
            value = 1.0 / k
        else:
            value = reached[k] / shown[k] if shown[k] else last
        if k == 5 and all(explored[r] >= MIN_EXPLORED for r in EXPLORE_RANKS) and explored_clicks[4]:
            ratio = (explored_clicks[5] / explored[5]) / (explored_clicks[4] / explored[4])
            value = out[4] * ratio
        last = max(min(value, last), PROPENSITY_FLOOR)
        out[k] = last if k > 1 else 1.0
    return out


def repeats_holdout(query: str, holdout: list[set[str]]) -> bool:
    """Whether ``query`` repeats one of the ``holdout`` questions (their ``terms``): it holds HOLDOUT_OVERLAP of the
    question's words, and they are at least half of its own."""
    words = set(terms(query))
    if not words:
        return False
    return any(len(words & q) >= HOLDOUT_OVERLAP * len(q) and 2 * len(words & q) >= len(words) for q in holdout if q)


class Nodes:
    """How a label's ids and paths name the graph's nodes: an id as the text printed it or as stored (``refs``), and a
    path by the File or Document node whose path it ends with, the longest such path when several, none when two of
    that length are."""

    def __init__(self, known: set[str], paths: Mapping[str, list[str]]):
        self.known = known
        self.paths = paths

    @classmethod
    def of_store(cls, store, b: int) -> Nodes:
        known = {
            row[0]
            for row in store.db.execute(
                "SELECT node_id FROM nodes WHERE tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)", (b, b)
            )
        }
        paths: dict[str, list[str]] = defaultdict(list)
        for node in store.path_nodes(b):
            paths[node["props"]["path"].strip("/")].append(node["id"])
        return cls(known, paths)

    def by_id(self, ident) -> str | None:
        if not isinstance(ident, str):
            return None
        return next((s for s in (ident, *refs.stored(ident)) if s in self.known), None)

    def by_path(self, path) -> str | None:
        if not isinstance(path, str) or not path.strip("/"):
            return None
        parts = path.strip("/").split("/")
        for start in range(len(parts)):
            found = self.paths.get("/".join(parts[start:]))
            if found:
                return found[0] if len(found) == 1 else None
        return None


def rows(labels: Iterable[Label], nodes: Nodes, now: datetime, holdout: Iterable[str] = ()) -> list[Row]:
    """The rows of the asked index the labels give (the module's docstring), ordered by node and label."""
    labels = list(labels)
    held = [set(terms(q)) for q in holdout]
    labels = [label for label in labels if not repeats_holdout(label.query, held)]
    exam = propensities(labels)
    words: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    total: dict[tuple, float] = defaultdict(float)
    for label in labels:
        found = terms(label.query)
        if not found:
            continue
        age = (now - label.when).total_seconds() / 86400 if label.when is not None else HALF_LIFE_DAYS
        fade = decay(age)
        weights: dict[str, float] = {}
        for click in label.clicks:
            rank = _rank(click)
            node = nodes.by_id(click.get("id")) or nodes.by_path(click.get("path"))
            if rank is None or node is None:
                continue
            weights[node] = max(weights.get(node, 0.0), 1.0 / max(exam.get(rank, PROPENSITY_FLOOR), PROPENSITY_FLOOR))
        for path in label.after_grep:
            node = nodes.by_path(path)
            if node is not None:
                weights[node] = max(weights.get(node, 0.0), GREP_WEIGHT)
        for node, weight in weights.items():
            key = (node, label.level, label.location, label.audience)
            total[key] += weight * fade
            for term in found:
                words[key][term] += weight * fade
    out = []
    for key in sorted(total, key=lambda k: (k[0], k[1], k[2], k[3] or "")):
        heaviest = sorted(words[key].items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_WORDS]
        text = " ".join(" ".join([term] * min(max(round(w), 1), MAX_REPEAT)) for term, w in heaviest)
        out.append(Row(key[0], key[1], key[2], key[3], round(prior(total[key]), 4), text))
    return out


def audience_text(audience) -> str | None:
    """An audience as ``label_aud`` stores it: None for none, else the JSON of its sorted principals."""
    return json.dumps(sorted(audience)) if audience else None
