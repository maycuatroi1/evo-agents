"""The text index of a snapshot (``evo_agents.kg.snapshot``) and the ranking of a query on it: ``Store.search_scored``
(``evo_agents.kg.store_rank``) over in-RAM tables (``evo_agents.kg.snapshot_fts``) in place of the store's FTS5 ones.

``TextIndex`` holds the rows the store writes to its full-text tables (``store_fts.search_rows``): those without an
audience (``node_fts``), those with one (``node_fts_aud``), the trigrams of the name and path of each node's own row
without one (``node_tri``), and the text of code files (``code_fts``). ``search_scored`` reads a query against them
the way the store does, in the same order of ways:

1. the nodes whose id, alias or name is the query (``exact``);
2. BM25 of every word whole, of every word as a prefix, then of any word, each stage read while the ones before
   found fewer than asked, times the prior by kind, a passage's row weighed down, each node at its best row; a row
   with an audience is read only for a reader within it, scored with the IDF of the rows without one;
3. the nodes and files a word of the query names (``search.named_words``), at the best text score;
4. the nearest nodes by meaning given the query's vector (``Vectors``), and the nodes whose name or path holds the
   query's long words as substrings;
5. ``search.weight`` and closed plans, then reciprocal rank fusion (``semantic.fuse``).

What agents asked before they read a node (the hub's ``node_asked``) is not in a snapshot: its prior is 1."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from evo_agents.kg import search as text_search
from evo_agents.kg import semantic
from evo_agents.kg.policy import audience_below
from evo_agents.kg.snapshot_fts import Table, code_words, trigrams, words
from evo_agents.kg.store_filters import stored_audience

IndexRow = tuple[str, str, str]  # (kind, name, path) of a node's own row, as ``Store._index_row`` reads it


@dataclass(frozen=True)
class Vectors:
    """The vectors of a graph's rows (``evo_agents.kg.semantic``), one per row, each with its node, kind and
    audience."""

    info: semantic.Info
    node_ids: tuple[str, ...]
    kinds: tuple[str, ...]
    auds: tuple[str | None, ...]
    rows: tuple

    def nearest(self, vector, limit: int, kinds: Sequence[str], readable) -> list[tuple[str, float]]:
        """(node id, dot product) of the ``limit`` nodes nearest ``vector``, a node at its best row, of ``kinds``
        when given, a row with an audience only when ``readable`` says so (``semantic.nearest``)."""
        q = [float(x) for x in vector]
        scores = [sum(a * b for a, b in zip(row, q, strict=False)) for row in self.rows]
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        wanted = set(kinds or ())
        out: dict[str, float] = {}
        for i in order:
            if len(out) >= limit:
                break
            nid = self.node_ids[i]
            if nid in out or (wanted and self.kinds[i] not in wanted):
                continue
            if self.auds[i] is not None and not readable(self.auds[i]):
                continue
            out[nid] = scores[i]
        return list(out.items())


@dataclass(frozen=True)
class Row:
    key: str
    node_id: str
    kind: str
    name: str
    path: str
    aud: str | None

    @property
    def own(self) -> bool:
        return self.key == self.node_id


class TextIndex:
    """The full-text rows of one build, in RAM; see the module's docstring."""

    def __init__(self, rows: list[Row], public: Table, audience: Table, tri: Table, tri_rows, code: Table, code_ids):
        self.rows = rows
        self.public_rows = [r for r in rows if r.aud is None]
        self.audience_rows = [r for r in rows if r.aud is not None]
        self.public, self.audience, self.tri, self.tri_rows = public, audience, tri, tri_rows
        self.code, self.code_ids = code, code_ids
        self.own = {r.node_id: r for r in self.public_rows if r.own}
        self.files: dict[str, list[Row]] = {}
        for r in self.public_rows:
            if r.kind == "File" and r.path:
                pure = PurePosixPath(r.path)
                for word in {pure.name, pure.stem}:
                    self.files.setdefault(word, []).append(r)

    @classmethod
    def build(cls, keyed: list[tuple[str, tuple]], code: dict[str, str], folded: bool = False) -> TextIndex:
        """The index of ``keyed`` (key, (node id, kind, name, path, body[, audience])), as ``store_fts.search_rows``
        gives them, and of ``code`` (file node id: text; already folded when ``folded``)."""
        rows, public, audience = [], [], []
        for key, row in keyed:
            aud = next(iter(row[5:6]), None)
            rows.append(Row(key, row[0], row[1], row[2] or "", row[3] or "", aud))
            (public if aud is None else audience).append((row[2] or "", row[3] or "", row[4] or ""))
        tri_rows = [r for r in rows if r.aud is None and r.own]
        tri = Table(
            [(text_search.tri_text(r.name.split("\n", 1)[0]), text_search.tri_text(r.path)) for r in tri_rows], trigrams
        )
        code_ids = tuple(sorted(code))
        code_table = Table([(code[nid] if folded else text_search.fold(code[nid]),) for nid in code_ids], code_words)
        return cls(rows, Table(public, words), Table(audience, words), tri, tri_rows, code_table, code_ids)

    def index_row(self, node_id: str) -> IndexRow:
        r = self.own.get(node_id)
        return (r.kind, r.name.split("\n", 1)[0], r.path) if r else ("", "", "")

    def code_text(self, node_id: str) -> str | None:
        from bisect import bisect_left

        i = bisect_left(self.code_ids, node_id)
        return self.code.rows[i][0] if i < len(self.code_ids) and self.code_ids[i] == node_id else None


class Ranker:
    """``Store.search_scored`` on a snapshot view: ``view`` gives the reader, its tuning and its nodes."""

    def __init__(self, view):
        self.view = view
        self.text: TextIndex = view.snap.text
        self.weights = tuple(view.tuning.bm25[2:])

    def search(self, query: str, b: int = 0, limit: int = 50, **kw) -> list[str]:
        return [node_id for node_id, _ in self.search_scored(query, b, limit, **kw)]

    def search_scored(
        self,
        query: str,
        b: int = 0,
        limit: int = 50,
        *,
        kinds: list[str] | None = None,
        any_word: bool = True,
        deprecated: bool = True,
        vector=None,
    ) -> list[tuple[str, float | None]]:
        """(id, text score) of the nodes matching a query, best first, at most ``limit * 3``, as the store gives."""
        want = limit * 3
        kinds = sorted({k for k in kinds or [] if isinstance(k, str)})
        hits = self.view.exact(query, b, want, kinds)
        words_of = text_search.query_words(query)
        scores, rows = self._by_words(words_of, hits, want, kinds, any_word)
        named = text_search.named_words(query)
        self._by_names(query, named, (want, kinds), hits, (scores, rows))
        near = self._semantic_ranked(vector, kinds, set(hits)) if vector is not None else {}
        tri = self._trigram_ranked(words_of, want, kinds, set(hits))
        for node_id in (near.keys() | tri.keys()) - rows.keys():
            rows[node_id] = self.text.index_row(node_id)
        self._weigh(query, named, rows, (scores, near, tri))
        found = [*hits, *(h for h in self._fused(scores, near, tri, want) if h not in hits)]
        if not deprecated:
            gone = self.view.deprecated(found)
            found = [h for h in found if h not in gone]
        fused = bool(near)
        return [(h, None if fused or h in hits or h not in scores else -scores[h]) for h in found[:want]]

    def _by_words(self, words_of, hits, want, kinds, any_word) -> tuple[dict[str, float], dict[str, IndexRow]]:
        queries = list(text_search.fts_queries(words_of))
        scores: dict[str, float] = {}
        rows: dict[str, IndexRow] = {}
        for weight, fts_query in zip(text_search.STAGE_WEIGHTS, queries if any_word else queries[:2], strict=False):
            if len(hits) + len(scores.keys() - set(hits)) >= want:
                break
            for node_id, score, row in self.ranked(fts_query, want, kinds):
                scores[node_id] = min(score * weight, scores.get(node_id, math.inf))
                rows.setdefault(node_id, row)
        return scores, rows

    def _by_names(self, query, named, pool, hits, found) -> None:
        (want, kinds), (scores, rows) = pool, found
        top = min(scores.values(), default=-1.0)
        for word in named if query.strip() not in named else ():
            for node_id in self.view.exact(word, 0, want, kinds):
                if node_id not in scores and node_id not in hits:
                    scores[node_id], rows[node_id] = top, self.text.index_row(node_id)
        for word in named:
            for node_id, row in self.files_named(word, kinds):
                if node_id not in hits:
                    scores[node_id], rows[node_id] = min(top, scores.get(node_id, top)), row

    def files_named(self, word: str, kinds: list[str]) -> list[tuple[str, IndexRow]]:
        """(node id, (kind, name, path)) of the files whose file name or stem is ``word`` (``Store._files_named``)."""
        if kinds and "File" not in kinds:
            return []
        found = self.text.files.get(word, [])[: text_search.RERANK_POOL]
        return [(r.node_id, (r.kind, r.name, r.path)) for r in found if text_search.file_named(r.path, word)]

    def _readable(self, aud: str | None) -> bool:
        return aud is None or (bool(self.view.reader) and audience_below(stored_audience(aud), self.view.reader))

    def ranked(self, fts_query: str, limit: int, kinds: list[str]) -> list[tuple[str, float, IndexRow]]:
        """(node id, score, (kind, name, path)) of the nodes matching an FTS5 query, best first, as ``Store._ranked``
        gives them."""
        pool = max(limit, text_search.RERANK_POOL)
        wanted = set(kinds)
        text = self.text
        found: list[tuple[Row, float]] = []
        public = text.public.search(fts_query, self.weights, lambda n: not wanted or text.public_rows[n].kind in wanted)
        found += [
            (text.public_rows[n], s) for n, s in sorted(public, key=lambda x: (x[1], text.public_rows[x[0]].node_id))
        ][:pool]
        if self.view.reader and text.audience_rows:
            found += self._audience(fts_query, pool, wanted)
        weight, prior = text_search.PASSAGE_WEIGHT, text_search.PRIOR
        about = {r.node_id: (r.kind, r.name.split("\n", 1)[0], r.path) for r, _ in found}
        scored = sorted(
            ((r.node_id, s * prior.get(r.kind, 1.0) * (1.0 if r.own else weight)) for r, s in found),
            key=lambda x: (x[1], x[0]),
        )
        best: dict[str, float] = {}
        for node_id, score in scored:
            best.setdefault(node_id, score)
        return [(node_id, score, about[node_id]) for node_id, score in best.items()][:limit]

    def _audience(self, fts_query: str, pool: int, wanted: set[str]) -> list[tuple[Row, float]]:
        """The best ``pool`` rows with an audience the reader reads, by their own BM25, each scored again with the IDF
        the rows without one give each phrase (``Store._as_public``)."""
        from evo_agents.kg.snapshot_fts import Phrase

        text = self.text
        rows = text.audience_rows

        def keep(n: int) -> bool:
            return (not wanted or rows[n].kind in wanted) and self._readable(rows[n].aud)

        first = sorted(text.audience.search(fts_query, self.weights, keep), key=lambda x: (x[1], rows[x[0]].node_id))
        first = first[:pool]
        phrases = [
            Phrase(p.rstrip("*")[1:-1].replace('""', '"'), p.endswith("*")) for p in text_search.phrases(fts_query)
        ]
        scores = dict.fromkeys((n for n, _ in first), 0.0)
        for phrase in phrases:
            public_hits, own_hits = text.public.hits(phrase), text.audience.hits(phrase)
            idf = text.public.idf(len(public_hits))
            for n in scores:
                part = text.audience.score(n, [own_hits], [text.audience.idf(len(own_hits))], self.weights)
                if n in own_hits:
                    scores[n] += part * idf / text.audience.idf(len(own_hits))
        return [(rows[n], scores[n]) for n, _ in first]

    def _trigram_ranked(self, words_of, limit, kinds, hits) -> dict[str, float]:
        fts_query = text_search.trigram_query(words_of)
        if fts_query is None or not self.text.tri_rows:
            return {}
        rows = self.text.tri_rows
        wanted = set(kinds)
        found = self.text.tri.search(fts_query, self.weights[:2], lambda n: not wanted or rows[n].kind in wanted)
        found = sorted(found, key=lambda x: (x[1], rows[x[0]].node_id))[: max(limit, text_search.RERANK_POOL)]
        prior = text_search.PRIOR
        return {rows[n].node_id: s * prior.get(rows[n].kind, 1.0) for n, s in found if rows[n].node_id not in hits}

    def _semantic_ranked(self, vector, kinds, hits) -> dict[str, float]:
        vectors = self.view.snap.vectors
        if vectors is None:
            return {}
        try:
            near = vectors.nearest(
                vector,
                semantic.SEMANTIC_POOL + len(hits),
                kinds,
                lambda aud: self._readable(aud) and bool(self.view.reader),
            )
        except (TypeError, ValueError):
            return {}
        return {node_id: score for node_id, score in near if node_id not in hits and score > 0}

    def _closed_plans(self, ids: list[str]) -> set[str]:
        plans = {i.split("/step:", 1)[0] for i in ids}
        closed = set()
        for plan in self.view.nodes(sorted(plans)).values():
            props = plan["props"]
            if plan["kind"] == "Plan" and isinstance(props.get("steps"), int) and props["steps"] > 0:
                if props.get("done") == props["steps"]:
                    closed.add(plan["id"])
        return {i for i in ids if i.split("/step:", 1)[0] in closed}

    def _weigh(self, query, named, rows, ways) -> None:
        scores, near, tri = ways
        closed = self._closed_plans([i for i, row in rows.items() if row[0] in ("Plan", "PlanStep")])
        wanted = text_search.asked(query)
        for node_id, (kind, name, path) in rows.items():
            factor = text_search.weight(node_id, kind, name, path, named, wanted, node_id in closed)
            if node_id in tri and factor < 1:
                del tri[node_id]
            for way in (scores, near, tri):
                if node_id in way:
                    way[node_id] *= factor

    def _fused(self, scores, near, tri, want) -> list[str]:
        ranked = sorted(scores, key=lambda node_id: (scores[node_id], node_id))
        lists = [(1.0, ranked)]
        if tri:
            lists.append((text_search.TRIGRAM_WEIGHT, sorted(tri, key=lambda node_id: (tri[node_id], node_id))[:want]))
        if near:
            lists.append(
                (self.view.tuning.semantic_weight, sorted(near, key=lambda node_id: (-near[node_id], node_id)))
            )
        return semantic.fuse(lists, semantic.RRF_K) if len(lists) > 1 else ranked

    # -- code --------------------------------------------------------------------------------------------------------

    def code_files(self, words_of: list[str], limit: int = text_search.CODE_POOL) -> list[tuple[str, int]]:
        """(file node id, how many of ``words_of`` its text holds), the most words first (``Store.code_files``)."""
        held: dict[str, list] = {}
        ids = self.text.code_ids
        for word in words_of:
            rows = self.text.code.search(text_search.phrase(text_search.fold(word)), (1.0,))
            if len(rows) > limit:
                continue
            for n, score in rows:
                found = held.setdefault(ids[n], [0, 0.0])
                found[0] += 1
                found[1] = min(found[1], score)
        return [(nid, held[nid][0]) for nid in sorted(held, key=lambda nid: (-held[nid][0], held[nid][1], nid))]

    def code_lines(self, node_id: str, words_of: list[str], most: int = text_search.CODE_LINES) -> list[int]:
        text = self.text.code_text(node_id)
        return text_search.lines_holding(text, words_of, most) if text else []
