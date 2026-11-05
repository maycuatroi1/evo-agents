"""The ranking of the graph store (``evo_agents.kg.store``): a query's nodes by their id, alias or name, then by BM25
over the full-text tables times the prior and the weights of ``evo_agents.kg.search``, the matches of what agents
asked, the substrings of ``node_tri`` and, given a query's embedding, the nearest nodes by meaning
(``evo_agents.kg.semantic``), fused into one list."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.kg import search as text_search
from evo_agents.kg import semantic
from evo_agents.kg.store_asked import AskedIndex
from evo_agents.kg.store_filters import FTS_SEEN, live, seen, within
from evo_agents.kg.store_fts import FullText
from evo_agents.kg.store_schema import FTS_TABLES

IndexRow = tuple[str, str, str]  # (kind, name, path) of a node's own row in the full-text index


class Ranking(AskedIndex, FullText):
    """The methods of ``Store`` that rank the nodes of a query."""

    path: Path
    _vectors: semantic.Info | None

    if TYPE_CHECKING:

        def nodes(self, ids, b: int) -> dict[str, dict]: ...

    def search(
        self,
        query: str,
        b: int,
        limit: int = 50,
        *,
        kinds: list[str] | None = None,
        any_word: bool = True,
        deprecated: bool = True,
        vector=None,
    ) -> list[str]:
        """The ids of ``search_scored``, best first."""
        found = self.search_scored(
            query, b, limit, kinds=kinds, any_word=any_word, deprecated=deprecated, vector=vector
        )
        return [node_id for node_id, _ in found]

    def search_scored(
        self,
        query: str,
        b: int,
        limit: int = 50,
        *,
        kinds: list[str] | None = None,
        any_word: bool = True,
        deprecated: bool = True,
        vector=None,
    ) -> list[tuple[str, float | None]]:
        """(id, text score) of the nodes matching a query, best first, at most ``limit * 3``: a node whose id, alias or name is the
        query, then the full-text matches of all its words, of all its words as prefixes, and (``any_word``) of any
        of its words, ranked together by BM25 times the prior and the weight of the way that scores them best, times
        ``search.weight``: up when a word of the query names the node, down when it is background the query does not
        ask for (``evo_agents.kg.search``). A node a word names (``search.named_words``) that no text query found ranks
        as the best of them would. ``kinds`` keeps only nodes of those kinds; ``deprecated`` false leaves out the nodes
        a decision deprecated before the list is cut, so they take no room. ``vector``, the query's embedding
        (``evo_agents.kg.semantic.query_vector``), fuses this ranking by words with the graph's ranking by meaning,
        weighed the same way; the exact hits stay first."""
        want = limit * 3
        kinds = sorted({k for k in kinds or [] if isinstance(k, str)})
        hits = self.exact(query, b, want, kinds)
        words = text_search.query_words(query)
        scores, rows = self._by_words(words, hits, want, kinds, any_word)
        named = text_search.named_words(query)
        self._by_names(query, named, b, (want, kinds), hits, (scores, rows))
        self._by_asked(query, kinds, hits, (scores, rows))
        near = self._semantic_ranked(vector, kinds, set(hits)) if vector is not None else {}
        tri = self._trigram_ranked(words, want, kinds, set(hits))
        for node_id in (near.keys() | tri.keys()) - rows.keys():
            rows[node_id] = self._index_row(node_id)
        self._weigh(query, b, named, rows, (scores, near, tri))
        found = [*hits, *(h for h in self._fused(scores, near, tri, want) if h not in hits)]
        if not deprecated:
            gone = self._deprecated(found, b)
            found = [h for h in found if h not in gone]
        # The score: BM25 as a positive number (more is better) at the node's best way, the prior and the weights; None
        # for an exact hit, which comes first whatever it scores, and for every node when a ranking by meaning fused in.
        fused = bool(near)
        return [(h, None if fused or h in hits or h not in scores else -scores[h]) for h in found[:want]]

    def _by_words(
        self, words: list[str], hits: list[str], want: int, kinds: list[str], any_word: bool
    ) -> tuple[dict[str, float], dict[str, IndexRow]]:
        """The scores of ``search_scored``'s full-text ways (lower is better: BM25 is negative), each node at its best
        way, and the (kind, name, path) the index holds of each node scored; it stops once ``want`` nodes are found."""
        queries = list(text_search.fts_queries(words))
        scores: dict[str, float] = {}
        rows: dict[str, IndexRow] = {}  # node id: (kind, name, path) as the index holds them
        for weight, fts_query in zip(text_search.STAGE_WEIGHTS, queries if any_word else queries[:2], strict=False):
            if len(hits) + len(scores.keys() - set(hits)) >= want:
                break
            for node_id, score, row in self._ranked(fts_query, want, kinds):
                scores[node_id] = min(score * weight, scores.get(node_id, math.inf))  # bm25 is negative
                rows.setdefault(node_id, row)
        return scores, rows

    def _by_names(
        self,
        query: str,
        named: list[str],
        b: int,
        pool: tuple[int, list[str]],
        hits: list[str],
        found: tuple[dict[str, float], dict[str, IndexRow]],
    ) -> None:
        """Add to ``found`` (scores, rows) the nodes and files a word of the query names (``search.named_words``), each
        scored as the best text match: ``pool`` is (how many to look up, the kinds kept)."""
        (want, kinds), (scores, rows) = pool, found
        top = min(scores.values(), default=-1.0)
        for word in named if query.strip() not in named else ():
            for node_id in self.exact(word, b, want, kinds):
                if node_id not in scores and node_id not in hits:
                    scores[node_id], rows[node_id] = top, self._index_row(node_id)
        for word in named:
            for node_id, row in self._files_named(word, kinds):
                if node_id not in hits:  # the file a word names ranks as the best text match would
                    scores[node_id], rows[node_id] = min(top, scores.get(node_id, top)), row

    def _by_asked(
        self, query: str, kinds: list[str], hits: list[str], found: tuple[dict[str, float], dict[str, IndexRow]]
    ) -> None:
        """Add to ``found`` (scores, rows) the asked matches of ``query`` (``_asked_scores``), each weighed against the
        best text score: added to a node's score, or the score of a node of ``kinds`` no other way found."""
        scores, rows = found
        asked = self._asked_scores(query)
        best = min(scores.values(), default=-1.0)  # an asked match is weighed against the best text score
        for node_id, match in asked.items():
            score = self.weight_of_asked() * match * best
            if node_id in scores:
                scores[node_id] += score
            elif node_id not in hits:
                row = self._index_row(node_id)
                if row[0] and (not kinds or row[0] in kinds):
                    scores[node_id], rows[node_id] = score, row

    def _weigh(
        self,
        query: str,
        b: int,
        named: list[str],
        rows: dict[str, IndexRow],
        ways: tuple[dict[str, float], dict[str, float], dict[str, float]],
    ) -> None:
        """Multiply each node's score in ``ways`` (by words, by meaning, by substrings) by ``search.weight`` and its
        asked prior; a node that is background leaves the ranking by substrings."""
        scores, near, tri = ways
        closed = self._closed_plans(b, [i for i, row in rows.items() if row[0] in ("Plan", "PlanStep")])
        wanted = text_search.asked(query)
        priors = self._asked_priors(list(rows))
        for node_id, (kind, name, path) in rows.items():
            factor = text_search.weight(node_id, kind, name, path, named, wanted, node_id in closed)
            if node_id in tri and factor < 1:  # background: first in that list, its weight would not hold it back
                del tri[node_id]
            factor *= priors.get(node_id, 1.0)
            if node_id in scores:
                scores[node_id] *= factor
            if node_id in near:
                near[node_id] *= factor
            if node_id in tri:
                tri[node_id] *= factor

    def _fused(self, scores: dict[str, float], near: dict[str, float], tri: dict[str, float], want: int) -> list[str]:
        """The nodes of ``scores`` best first, fused with the ranking by substrings and by meaning when there is one."""
        ranked = sorted(scores, key=lambda node_id: (scores[node_id], node_id))
        lists = [(1.0, ranked)]
        if tri:
            by_substring = sorted(tri, key=lambda node_id: (tri[node_id], node_id))[:want]
            lists.append((text_search.TRIGRAM_WEIGHT, by_substring))
        if near:
            lists.append((self.tuning.semantic_weight, sorted(near, key=lambda node_id: (-near[node_id], node_id))))
        if len(lists) > 1:
            ranked = semantic.fuse(lists, semantic.RRF_K)
        return ranked

    def exact(self, query: str, b: int, limit: int, kinds: Sequence[str] = ()) -> list[str]:
        """Live nodes whose id, alias or name is ``query``, by id; each through an index, never a scan of nodes."""
        params = {"q": query, "b": b, "lim": limit, **{f"k{i}": k for i, k in enumerate(kinds)}}
        of_kind = f" AND nodes.kind IN ({', '.join(f':k{i}' for i in range(len(kinds)))})" if kinds else ""
        where = f"{live('nodes')}{of_kind} AND {seen('nodes')}"
        rows = self.db.execute(
            f"SELECT node_id FROM nodes WHERE node_id = :q AND {where}"
            f" UNION SELECT node_id FROM nodes WHERE name = :q AND {where}"
            " UNION SELECT nodes.node_id FROM node_alias JOIN nodes ON nodes.node_id = node_alias.node_id"
            f" WHERE node_alias.alias = :q AND {where} ORDER BY 1 LIMIT :lim",
            params,
        )
        return [r[0] for r in rows]

    def _ranked(self, fts_query: str, limit: int, kinds: list[str]) -> list[tuple[str, float, IndexRow]]:
        """(node id, score, (kind, name, path)) of the nodes matching an FTS5 query, best first: the best
        ``RERANK_POOL`` by BM25, scored again with the prior by kind (``evo_agents.kg.search``); [] for a query FTS5
        cannot read. The name is the node's as written, without the parts of its camelCase words. The rows of
        ``node_fts_aud`` are read only for a reader with an audience, and only those within it, each with the score
        ``node_fts`` would give it (``_as_public``)."""
        of_kind = f" AND kind IN ({', '.join('?' for _ in kinds)})" if kinds else ""
        weights = ", ".join(str(w) for w in self.tuning.bm25)
        rows = []
        for table in FTS_TABLES if self.reader else FTS_TABLES[:1]:
            aud_seen = FTS_SEEN if table == "node_fts_aud" else ""
            try:
                found = self.db.execute(
                    f"SELECT node_id, kind, bm25({table}, {weights}) AS score, rowid, name, path FROM {table}"
                    f" WHERE {table} MATCH ?{of_kind}{aud_seen} ORDER BY score, node_id LIMIT ?",
                    (fts_query, *kinds, max(limit, text_search.RERANK_POOL)),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            rows += self._as_public(fts_query, found) if table == "node_fts_aud" else found
        prior = text_search.PRIOR
        own = self._own_rows({r[0] for r in rows})
        weight = text_search.PASSAGE_WEIGHT
        about = {r[0]: (r[1], str(r[4] or "").split("\n", 1)[0], r[5] or "") for r in rows}
        scored = sorted(
            ((r[0], r[2] * prior.get(r[1], 1.0) * (1.0 if own.get(r[0]) == r[3] else weight)) for r in rows),
            key=lambda r: (r[1], r[0]),
        )
        best: dict[str, float] = {}
        for node_id, score in scored:  # a node's own row and its passages: the best of them
            best.setdefault(node_id, score)
        return [(node_id, score, about[node_id]) for node_id, score in best.items()][:limit]  # lower is better

    def _own_rows(self, ids: set[str]) -> dict[str, int]:
        """The rowid of the own row of each node of ``ids`` in the full-text index, which its passages' rows are not."""
        out: dict[str, int] = {}
        ordered = sorted(ids)
        for i in range(0, len(ordered), 500):
            chunk = ordered[i : i + 500]
            marks = ", ".join("?" for _ in chunk)
            out.update(
                self.db.execute(f"SELECT node_id, fts_rowid FROM node_fts_rows WHERE node_id IN ({marks})", chunk)
            )
        return out

    def _as_public(self, fts_query: str, rows: list[tuple]) -> list[tuple]:
        """``rows`` of ``node_fts_aud`` (node id, kind, BM25, rowid, name, path) with the BM25 ``node_fts`` would give them. BM25
        adds up, over the phrases of the query, the phrase's IDF times a part that only the row's own counts and
        length make. ``node_fts_aud`` holds few rows (a project's chat messages), so the IDF it gives a word is a
        fraction of what ``node_fts`` gives it, and a message would rank below any symbol or section that matches no
        better. Each phrase's part is read back from the BM25 of the phrase alone and weighed with the phrase's IDF
        in ``node_fts``, so no row of an audience the reader lacks weighs in it through the IDF either."""
        if not rows:
            return rows
        weights = ", ".join(str(w) for w in self.tuning.bm25)
        marks = ", ".join("?" for _ in rows)
        held = dict(self.db.execute("SELECT aud, count(*) FROM node_fts_rows GROUP BY aud").fetchall())
        totals = {table: held.get(aud, 0) for aud, table in enumerate(FTS_TABLES)}  # its rows, as _update_fts keeps
        scores = dict.fromkeys((r[3] for r in rows), 0.0)
        for phrase in text_search.phrases(fts_query):
            weight = self._idf("node_fts", phrase, totals) / self._idf("node_fts_aud", phrase, totals)
            for rowid, part in self.db.execute(
                f"SELECT rowid, bm25(node_fts_aud, {weights}) FROM node_fts_aud WHERE node_fts_aud MATCH ?"
                f" AND rowid IN ({marks})",
                (phrase, *scores),
            ):
                scores[rowid] += part * weight
        return [(r[0], r[1], scores[r[3]], *r[3:]) for r in rows]

    def _idf(self, table: str, phrase: str, totals: dict[str, int]) -> float:
        """The IDF FTS5's bm25 gives ``phrase`` in ``table``: ln((N - n + 0.5) / (n + 0.5)) for its N rows, n of which
        hold the phrase, or 1e-6 when that is not above 0."""
        held = self.db.execute(f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (phrase,)).fetchone()[0]
        idf = math.log((totals[table] - held + 0.5) / (held + 0.5))
        return idf if idf > 0 else 1e-6

    def _trigram_ranked(self, words: list[str], limit: int, kinds: list[str], hits: set[str]) -> dict[str, float]:
        """BM25 times the prior by kind of the best RERANK_POOL nodes whose name or path holds every word of ``words``
        as a substring (``search.trigram_query``), not of ``hits``; lower is better, as in ``_ranked``. {} without
        ``node_tri`` or such a word."""
        fts_query = text_search.trigram_query(words)
        if fts_query is None or not self.has_trigram():
            return {}
        of_kind = f" AND kind IN ({', '.join('?' for _ in kinds)})" if kinds else ""
        weights = ", ".join(str(w) for w in self.tuning.bm25[:4])  # node_id, kind, name, path
        try:
            found = self.db.execute(
                f"SELECT node_id, kind, bm25(node_tri, {weights}) AS score FROM node_tri"
                f" WHERE node_tri MATCH ?{of_kind} ORDER BY score, node_id LIMIT ?",
                (fts_query, *kinds, max(limit, text_search.RERANK_POOL)),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        prior = text_search.PRIOR
        return {node_id: score * prior.get(kind, 1.0) for node_id, kind, score in found if node_id not in hits}

    # -- the ranking by meaning (evo_agents.kg.semantic) ------------------------------------------------------------

    def vector_info(self) -> semantic.Info | None:
        """What the graph's vectors are, None when it has none (a graph built without them, or by an older build)."""
        if not hasattr(self, "_vectors"):
            self._vectors = semantic.info(self.db)
        return self._vectors

    def _semantic_ranked(self, vector, kinds: list[str], hits: set[str]) -> dict[str, float]:
        """Cosine of the SEMANTIC_POOL nodes nearest ``vector``, not of ``hits``, rows with an audience only within the
        reader's; {} when the graph has no vectors or numpy is missing."""
        found = self.vector_info()
        if found is None:
            return {}
        try:
            stat = self.path.stat()
            key = (str(self.path.resolve()), stat.st_mtime_ns, stat.st_size, found.digest)
            m = semantic.matrix(self.db, found, key)
            reader = self.reader

            def readable(aud: str) -> bool:
                return bool(reader) and within(reader, aud)

            near = semantic.nearest(m, vector, semantic.SEMANTIC_POOL + len(hits), kinds, readable)
        except (semantic.SemanticUnavailable, ValueError, OSError):
            return {}
        return {node_id: score for node_id, score in near if node_id not in hits and score > 0}

    def _closed_plans(self, b: int, ids: list[str]) -> set[str]:
        """The ids of ``ids`` (plans and steps) whose plan is closed: it has steps and all of them are done."""
        plans = {i.split("/step:", 1)[0] for i in ids}
        closed = set()
        for plan in self.nodes(sorted(plans), b).values():
            props = plan["props"]
            if plan["kind"] == "Plan" and isinstance(props.get("steps"), int) and props["steps"] > 0:
                if props.get("done") == props["steps"]:
                    closed.add(plan["id"])
        return {i for i in ids if i.split("/step:", 1)[0] in closed}

    def _deprecated(self, ids: list[str], b: int) -> set[str]:
        """The ids of ``ids`` whose live node a decision deprecated (``props.deprecated``)."""
        out: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ", ".join("?" for _ in chunk)
            out.update(
                r[0]
                for r in self.db.execute(
                    f"SELECT node_id FROM nodes WHERE node_id IN ({marks}) AND tx_from <= ? AND (tx_to IS NULL OR"
                    " tx_to > ?) AND json_extract(props, '$.deprecated') IS NOT NULL",
                    (*chunk, b, b),
                )
            )
        return out
