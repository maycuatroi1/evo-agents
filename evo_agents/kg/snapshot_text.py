"""The text index of a snapshot (``evo_agents.kg.snapshot``) and the ranking of a query on it (``search_scored``),
over in-RAM tables (``evo_agents.kg.snapshot_fts``) that stand for FTS5 ones.

``TextIndex`` holds the rows of a build's full-text index (``graph_model.search_rows``): those without an
audience (``node_fts``), those with one (``node_fts_aud``), the trigrams of the name and path of each node's own row
without one (``node_tri``), and the text of code files (``code_fts``). ``search_scored`` reads a query against them
in this order of ways:

1. the nodes whose id, alias or name is the query (``exact``);
2. BM25 of every word whole, of every word as a prefix, then of any word, each stage read while the ones before
   found fewer than asked, times the prior by kind, a passage's row weighed down, each node at its best row; a row
   with an audience is read only for a reader within it, scored with the IDF of the rows without one;
3. the nodes and files a word of the query names (``search.named_words``), at the best text score;
4. the nearest nodes by meaning given the query's vector (``Vectors``), and the nodes whose name or path holds the
   query's long words as substrings;
5. ``search.weight`` and closed plans, then reciprocal rank fusion (``semantic.fuse``).

What agents asked before they read a node (``Snapshot.asked``) counts too (``evo_agents.kg.asked``): for a reader
whose ``asked_clearance`` is set, an asked match adds to a node's score and its prior multiplies it. A row matches
when it holds a word of the query, as FTS5 matches any of them."""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from evo_agents.kg import asked as asked_words
from evo_agents.kg import search as text_search
from evo_agents.kg import semantic, snapshot_fts
from evo_agents.kg.graph_model import within
from evo_agents.kg.pacing import paced
from evo_agents.kg.snapshot_fts import (
    Phrase,
    QueryError,
    Table,
    code_words,
    parse,
    trigrams,
    wand_top_k,
    words,
)

IndexRow = tuple[str, str, str]  # (kind, name, path) of a node's own row, as ``TextIndex.index_row`` reads it


_MATRIX_MADE = threading.Lock()  # one thread makes a snapshot's matrix of vectors


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
        if not self.node_ids:
            return []
        np = semantic._numpy()
        scores = self._matrix @ np.asarray(vector, dtype=np.float32)
        order = np.argsort(-scores, kind="stable")
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
            out[nid] = float(scores[i])
        return list(out.items())

    @property
    def _matrix(self):
        """The rows as one float32 matrix, made at the first search by meaning: a product of Python floats per row
        took seconds a question on a graph of tens of thousands of vectors (``semantic.nearest`` does the same). It
        takes the place of ``rows``, which only this search reads."""
        found = self.__dict__.get("_matrix_made")
        if found is None:
            with _MATRIX_MADE:  # a second thread searching meanwhile would read the rows already dropped
                found = self.__dict__.get("_matrix_made")
                if found is None:
                    np = semantic._numpy()
                    found = np.asarray(self.rows, dtype=np.float32)
                    self.__dict__["_matrix_made"] = found
                    object.__setattr__(self, "rows", ())  # the matrix holds them now: both held their memory twice
        return found


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
        self._kinds: dict[str, tuple] = {}  # per table, its rows' kinds as numbers (``kept``)

    def kept(self, table: str, wanted):
        """What keeps the rows of ``table`` ("public", "audience" or "tri") of a kind in ``wanted``: None for any kind,
        else a mask over them, or a callable of a row without numpy."""
        rows = {"public": self.public_rows, "audience": self.audience_rows, "tri": self.tri_rows}[table]
        if not wanted:
            return None
        np = snapshot_fts._np
        if np is None:
            return lambda n: rows[n].kind in wanted
        held = self._kinds.get(table)
        if held is None:
            numbers: dict[str, int] = {}
            codes = np.fromiter(
                (numbers.setdefault(r.kind, len(numbers)) for r in rows), dtype=np.int32, count=len(rows)
            )
            held = self._kinds[table] = (numbers, codes)
        numbers, codes = held
        return np.isin(codes, [numbers[k] for k in wanted if k in numbers])

    @classmethod
    def build(cls, keyed: list[tuple[str, tuple]], code: dict[str, str], folded: bool = False) -> TextIndex:
        """The index of ``keyed`` (key, (node id, kind, name, path, body[, audience])), as ``graph_model.search_rows``
        gives them, and of ``code`` (file node id: text; already folded when ``folded``)."""
        rows, public, audience = [], [], []
        for key, row in paced(keyed):
            aud = next(iter(row[5:6]), None)
            rows.append(Row(key, row[0], row[1], row[2] or "", row[3] or "", aud))
            (public if aud is None else audience).append((row[2] or "", row[3] or "", row[4] or ""))
        tri_rows = [r for r in rows if r.aud is None and r.own]
        tri = Table([tri_texts(r) for r in paced(tri_rows)], trigrams)
        code_ids = tuple(sorted(code))
        code_table = Table(
            [(code[nid] if folded else text_search.fold(code[nid]),) for nid in paced(code_ids)], code_words
        )
        return cls(rows, Table(public, words), Table(audience, words), tri, tri_rows, code_table, code_ids)

    def updated(self, keyed: Sequence[tuple[str, tuple]], gone, code: dict[str, str], code_gone) -> TextIndex:
        """The index with the rows of the keys ``gone`` dropped and the rows ``keyed`` (key, row as ``build`` reads
        it) added, and the code of the files ``code_gone`` dropped and ``code`` (folded) set: what ``build`` makes of
        the rows then, of this index's tables with the new rows alone cut into tokens (``Table.updated``). Raises
        ValueError when a key would be held twice."""
        from evo_agents.kg import snapshot_text_update

        return snapshot_text_update.updated(self, keyed, frozenset(gone), code, code_gone)

    def index_row(self, node_id: str) -> IndexRow:
        r = self.own.get(node_id)
        return (r.kind, r.name.split("\n", 1)[0], r.path) if r else ("", "", "")

    def code_text(self, node_id: str) -> str | None:
        from bisect import bisect_left

        i = bisect_left(self.code_ids, node_id)
        return self.code.rows[i][0] if i < len(self.code_ids) and self.code_ids[i] == node_id else None


def tri_texts(r: Row) -> tuple[str, str]:
    """A row's texts in the trigram table: the first line of its name, and its path."""
    return text_search.tri_text(r.name.split("\n", 1)[0]), text_search.tri_text(r.path)


class Ranker:
    """The search of a snapshot view: ``view`` gives the reader, its tuning and its nodes."""

    def __init__(self, view):
        self.view = view
        self.text: TextIndex = view.snap.text
        self.weights = tuple(view.tuning.bm25[2:])

    def search(self, query: str, *, limit: int = 50, **kw) -> list[str]:
        return [node_id for node_id, _ in self.search_scored(query, limit=limit, **kw)]

    def search_scored(
        self,
        query: str,
        *,
        limit: int = 50,
        kinds: list[str] | None = None,
        any_word: bool = True,
        deprecated: bool = True,
        vector=None,
    ) -> list[tuple[str, float | None]]:
        """(id, text score) of the nodes matching a query, best first, at most ``limit * 3``."""
        want = limit * 3
        kinds = sorted({k for k in kinds or [] if isinstance(k, str)})
        hits = self.view.exact(query, limit=want, kinds=kinds)
        words_of = text_search.query_words(query)
        scores, rows = self._by_words(words_of, hits, want, kinds, any_word)
        named = text_search.named_words(query)
        self._by_names(query, named, (want, kinds), hits, (scores, rows))
        self._by_asked(query, kinds, hits, (scores, rows))
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
            for node_id in self.view.exact(word, limit=want, kinds=kinds):
                if node_id not in scores and node_id not in hits:
                    scores[node_id], rows[node_id] = top, self.text.index_row(node_id)
        for word in named:
            for node_id, row in self.files_named(word, kinds):
                if node_id not in hits:
                    scores[node_id], rows[node_id] = min(top, scores.get(node_id, top)), row

    def _asked_rows(self):
        """The asked rows the reader may read within its ``asked_clearance``; none without one."""
        clearance = self.view.asked_clearance
        if clearance is None:
            return []
        lvl, loc = clearance
        return [r for r in self.view.snap.asked if r[1] <= lvl and r[2] <= loc and self._readable(r[3])]

    def _asked_scores(self, query: str) -> dict[str, float]:
        """Per node, how well the best of its readable asked rows matches ``query``."""
        if not self.view.snap.asked or not self.view.weight_of_asked():
            return {}
        words_of = asked_words.terms(query)
        wanted = set(words_of)
        out: dict[str, float] = {}
        for node_id, *_, text in self._asked_rows():
            if wanted.intersection(text.split()):
                out[node_id] = max(out.get(node_id, 0.0), asked_words.match(words_of, text))
        return out

    def _asked_priors(self, ids) -> dict[str, float]:
        """Per node of ``ids``, the highest prior of its readable asked rows."""
        ids = set(ids)
        out: dict[str, float] = {}
        for node_id, _, _, _, prior, _ in self._asked_rows() if ids else ():
            if node_id in ids:
                out[node_id] = max(out.get(node_id, prior), prior)
        return {k: min(max(v, 1.0), asked_words.PRIOR_MAX) for k, v in out.items()}

    def _by_asked(self, query, kinds, hits, found) -> None:
        """An asked match adds to a node's score, weighed against the best text score."""
        scores, rows = found
        asked = self._asked_scores(query)
        best = min(scores.values(), default=-1.0)
        for node_id, match in asked.items():
            score = self.view.weight_of_asked() * match * best
            if node_id in scores:
                scores[node_id] += score
            elif node_id not in hits:
                row = self.text.index_row(node_id)
                if row[0] and (not kinds or row[0] in kinds):
                    scores[node_id], rows[node_id] = score, row

    def files_named(self, word: str, kinds: list[str]) -> list[tuple[str, IndexRow]]:
        """(node id, (kind, name, path)) of the files whose file name or stem is ``word``."""
        if kinds and "File" not in kinds:
            return []
        found = self.text.files.get(word, [])[: text_search.RERANK_POOL]
        return [(r.node_id, (r.kind, r.name, r.path)) for r in found if text_search.file_named(r.path, word)]

    def matched(self, ids: list[str], query: str) -> dict[str, dict]:
        """What matched, without the passages: per node of ``ids`` with an own row the reader reads, how many of
        the query's words its name, path or body holds whole (``words``), of ``of``."""
        query_words = text_search.query_words(query)
        if not ids or not query_words:
            return {}
        wanted, out = set(ids), {}
        for table, rows in ((self.text.public, self.text.public_rows), (self.text.audience, self.text.audience_rows)):
            held = {n: 0 for n, r in enumerate(rows) if r.own and r.node_id in wanted and self._readable(r.aud)}
            if not held:
                continue
            for word in query_words:
                try:
                    phrases, tree = parse(next(text_search.fts_queries([word])))
                except QueryError:
                    continue
                for n in table.matching_rows(phrases, tree, set(held)):
                    held[n] += 1
            out.update({rows[n].node_id: {"words": count, "of": len(query_words)} for n, count in held.items()})
        return out

    def _readable(self, aud: str | None) -> bool:
        return aud is None or (bool(self.view.reader) and within(self.view.reader, aud))

    def ranked(self, fts_query: str, limit: int, kinds: list[str]) -> list[tuple[str, float, IndexRow]]:
        """(node id, score, (kind, name, path)) of the nodes matching an FTS5 query, best first."""
        pool = max(limit, text_search.RERANK_POOL)
        wanted = set(kinds)
        text = self.text
        found: list[tuple[Row, float]] = []
        public = wand_top_k(text.public, fts_query, self.weights, pool, text.kept("public", wanted))
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
        the rows without one give each phrase."""
        text = self.text
        rows = text.audience_rows
        kinds = text.kept("audience", wanted)

        def keep(n: int) -> bool:
            return (kinds is None or bool(kinds[n] if not callable(kinds) else kinds(n))) and self._readable(
                rows[n].aud
            )

        found = wand_top_k(text.audience, fts_query, self.weights, pool, keep)
        first = sorted(found, key=lambda x: (x[1], rows[x[0]].node_id))[:pool]
        phrases = [
            Phrase(p.rstrip("*")[1:-1].replace('""', '"'), p.endswith("*")) for p in text_search.phrases(fts_query)
        ]
        scores = dict.fromkeys((n for n, _ in first), 0.0)
        for phrase in phrases:
            idf = text.public.idf(text.public.count(phrase))
            held, own_hits = text.audience.held_by(phrase, scores)
            own_idf = text.audience.idf(held)
            for n in own_hits:
                part = text.audience.score(n, [own_hits], [own_idf], self.weights)
                scores[n] += part * idf / own_idf
        return [(rows[n], scores[n]) for n, _ in first]

    def _trigram_ranked(self, words_of, limit, kinds, hits) -> dict[str, float]:
        fts_query = text_search.trigram_query(words_of)
        if fts_query is None or not self.text.tri_rows:
            return {}
        rows = self.text.tri_rows
        wanted = set(kinds)
        pool = max(limit, text_search.RERANK_POOL)
        found = wand_top_k(self.text.tri, fts_query, self.weights[:2], pool, self.text.kept("tri", wanted))
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
        priors = self._asked_priors(rows)
        for node_id, (kind, name, path) in rows.items():
            factor = text_search.weight(node_id, kind, name, path, named, wanted, node_id in closed)
            if node_id in tri and factor < 1:
                del tri[node_id]
            factor *= priors.get(node_id, 1.0)
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
        """(file node id, how many of ``words_of`` its text holds), the most words first."""
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
