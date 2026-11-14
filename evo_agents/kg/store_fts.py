"""The full-text tables of the graph store (``evo_agents.kg.store``): ``node_fts`` and ``node_fts_aud``, the rows of
nodes and their passages without an audience and with one, ``node_fts_rows`` that keeps each row's rowid and digest,
``node_tri``, the trigrams of the name and path of each own row of ``node_fts``, ``code_fts``, the text of code files,
and the aliases ``node_alias``. They hold the latest build only, and a build rewrites only the rows that changed."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Set
from typing import TYPE_CHECKING

from evo_agents.kg import search as text_search
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store_filters import FTS_SEEN, label_columns
from evo_agents.kg.store_schema import FTS_TABLES, OWN_ROWS

if TYPE_CHECKING:
    from evo_agents.kg.store import Graph


def search_rows(graph: Graph) -> tuple[list[tuple], list[tuple[str, tuple]]]:
    """The full-text rows of ``graph``: every row as the digest reads it, and each keyed as ``node_fts_rows`` keeps it.
    A row of a node with an audience ends with it. A passage's row is its node's (id, kind, name, path) with the
    passage's text as body, keyed ``search.passage_key``."""
    rows: list[tuple] = []
    keyed: list[tuple[str, tuple]] = []
    for nid, n in sorted(graph.nodes.items()):
        aud = (label_columns(n["label"])["label_aud"],) if n["label"].audience else ()
        name, path = text_search.index_text(n["name"] or ""), text_search.fold(graph.paths.get(nid, ""))
        row = (nid, n["kind"], name, path, text_search.index_text(graph.texts.get(nid, ""))) + aud
        rows.append(row)
        keyed.append((nid, row))
        for p in graph.passages.get(nid, ()):
            key = text_search.passage_key(nid, p["start"], p["end"])
            passage = (nid, n["kind"], name, path, text_search.index_text(p["text"])) + aud
            rows.append((key, *passage))
            keyed.append((key, passage))
    return rows, keyed


def code_digest(code: dict[str, str]) -> str | None:
    """The digest of what ``code_fts`` holds for ``code``; None when it holds nothing."""
    return sha256(canonical_json(sorted((nid, sha256(text)) for nid, text in code.items()))) if code else None


def searched_digest(rows: list[tuple], code: str | None) -> str:
    """The digest of what the full-text index holds: its rows and the digest of its code."""
    return sha256(canonical_json(rows if code is None else {"nodes": rows, "code": code}))


class FullText:
    """The methods of ``Store`` that write and read the full-text tables."""

    db: sqlite3.Connection
    reader: frozenset[str]
    tuning: kg_settings.Tuning
    _trigram: bool | None

    def _write_search(self, graph: Graph) -> str:
        """Bring the full-text index to ``graph``'s nodes and their passages, deleting and inserting only the rows that
        changed, and replace the aliases; the digest of what the index holds. A row of a node with an audience ends
        with it. A passage's row is its node's (id, kind, name, path) with the passage's text as body, keyed
        ``search.passage_key``; a graph without passages digests as before they came."""
        rows, keyed = search_rows(graph)
        self._update_fts(keyed)
        code = self._update_code(graph.code)
        self.db.execute("DELETE FROM node_alias")
        self.db.executemany(
            "INSERT OR IGNORE INTO node_alias (alias, node_id) VALUES (?, ?)",
            sorted((alias, nid) for nid, n in graph.nodes.items() for alias in n.get("aliases", [])),
        )
        return searched_digest(rows, code)

    def _update_code(self, code: dict[str, str]) -> str | None:
        """Make ``code_fts`` hold the text of ``code`` (file node id: text) and nothing else, writing only the files
        whose text changed, as ``_update_fts`` does; the digest of what it holds, None when it holds nothing, so a
        graph without code text digests as before the index came."""
        db = self.db
        digests = {nid: sha256(text) for nid, text in code.items()}
        held = {nid: (rowid, d) for nid, rowid, d in db.execute("SELECT node_id, fts_rowid, digest FROM code_fts_rows")}
        if db.execute("SELECT count(*) FROM code_fts").fetchone()[0] != len(held):
            db.execute("DELETE FROM code_fts")
            db.execute("DELETE FROM code_fts_rows")
            held = {}
        gone = [(nid, rowid) for nid, (rowid, d) in held.items() if digests.get(nid) != d]
        db.executemany("DELETE FROM code_fts WHERE rowid = ?", [(rowid,) for _, rowid in gone])
        db.executemany("DELETE FROM code_fts_rows WHERE node_id = ?", [(nid,) for nid, _ in gone])
        fresh = sorted(nid for nid in code if held.get(nid, (0, None))[1] != digests[nid])
        last = db.execute("SELECT rowid FROM code_fts ORDER BY rowid DESC LIMIT 1").fetchone()
        start = (last[0] if last else 0) + 1
        db.executemany(
            "INSERT INTO code_fts (rowid, node_id, body) VALUES (?, ?, ?)",
            [(start + i, nid, text_search.fold(code[nid])) for i, nid in enumerate(fresh)],
        )
        db.executemany(
            "INSERT INTO code_fts_rows VALUES (?, ?, ?)",
            [(nid, start + i, digests[nid]) for i, nid in enumerate(fresh)],
        )
        return sha256(canonical_json(sorted(digests.items()))) if digests else None

    def _update_fts(self, keyed: list[tuple[str, tuple]]) -> None:
        """Make the full-text tables hold the rows of ``keyed`` (key, row) and nothing else, a row with an audience
        (six columns) in ``node_fts_aud``: the rows whose digest ``node_fts_rows`` does not hold under their key go, new
        and changed ones come in. An index whose rows ``node_fts_rows`` does not account for is written again whole. A
        node's own row is keyed by its id and digested as it always was; a passage's by ``search.passage_key``."""
        db = self.db
        digests = {key: sha256(canonical_json(row if key == row[0] else [key, *row])) for key, row in keyed}
        held = {
            key: (rowid, d, aud)
            for key, rowid, d, aud in db.execute("SELECT node_id, fts_rowid, digest, aud FROM node_fts_rows")
        }
        trigram = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'node_tri'").fetchone() is not None
        if sum(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in FTS_TABLES) != len(held):
            for table in (*FTS_TABLES, "node_fts_rows", *(("node_tri",) if trigram else ())):
                db.execute(f"DELETE FROM {table}")
            held = {}
        gone = [(key, rowid, aud) for key, (rowid, d, aud) in held.items() if digests.get(key) != d]
        tri_gone = [rowid for key, rowid, aud in gone if aud == 0 and text_search.PASSAGE_MARK not in key]
        tri_added: list[tuple] = []
        for aud, table in enumerate(FTS_TABLES):
            db.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(rowid,) for _, rowid, a in gone if a == aud])
        db.executemany("DELETE FROM node_fts_rows WHERE node_id = ?", [(key,) for key, _, _ in gone])
        fresh = [(key, row) for key, row in keyed if held.get(key, (0, None))[1] != digests[key]]
        for aud, table in enumerate(FTS_TABLES):
            mine = [(key, row) for key, row in fresh if (len(row) > 5) == bool(aud)]
            last = db.execute(f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            start = (last[0] if last else 0) + 1
            columns = "rowid, node_id, kind, name, path, body" + (", aud" if aud else "")
            marks = ", ".join("?" for _ in columns.split(", "))
            db.executemany(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                [(start + i, *row) for i, (_, row) in enumerate(mine)],
            )
            db.executemany(
                "INSERT INTO node_fts_rows VALUES (?, ?, ?, ?)",
                [(key, start + i, digests[key], aud) for i, (key, _) in enumerate(mine)],
            )
            if not aud:
                tri_added += [(start + i, *row[:4]) for i, (key, row) in enumerate(mine) if key == row[0]]
        if trigram:
            self._update_trigram(tri_gone, tri_added)

    def _update_trigram(self, gone: list[int], added: list[tuple]) -> None:
        """Bring ``node_tri`` to the own rows of ``node_fts`` (OWN_ROWS) after ``_update_fts``: delete the rowids of
        ``gone``, insert ``added`` (rowid, node id, kind, name, path), and write it again whole from ``node_fts`` when
        it does not then hold exactly those rows, under their rowids: a file written before it, or one a version
        without it built again."""
        db = self.db
        insert = "INSERT INTO node_tri (rowid, node_id, kind, name, path) VALUES (?, ?, ?, ?, ?)"
        db.executemany("DELETE FROM node_tri WHERE rowid = ?", [(rowid,) for rowid in gone])
        db.executemany(insert, [tri_row(row) for row in added])
        held = db.execute("SELECT count(*) FROM node_tri").fetchone()[0]
        stale = db.execute(
            f"SELECT count(*) FROM node_tri WHERE (rowid, node_id) NOT IN (SELECT fts_rowid, node_id FROM {OWN_ROWS})"
        ).fetchone()[0]
        if not stale and held == db.execute(f"SELECT count(*) FROM {OWN_ROWS}").fetchone()[0]:
            return
        db.execute("DELETE FROM node_tri")
        rows = db.execute(
            f"SELECT rowid, node_id, kind, name, path FROM node_fts WHERE rowid IN (SELECT fts_rowid FROM {OWN_ROWS})"
        )
        db.executemany(insert, [tri_row(row) for row in rows.fetchall()])

    def plain_search(self, query: str, limit: int, kinds: list[str] | None = None) -> list[str]:
        """Ids of the nodes plain BM25 ranks for ``query``, best first, at most ``limit``: a node at its best row, of
        ``kinds`` (default ``search.PLAIN_KINDS``), from the rows without an audience (``search.plain_query``)."""
        fts_query = text_search.plain_query(query)
        if fts_query is None:
            return []
        kinds = sorted({k for k in kinds or text_search.PLAIN_KINDS if isinstance(k, str)})
        weights = ", ".join(str(w) for w in text_search.PLAIN_WEIGHTS)
        try:
            rows = self.db.execute(
                f"SELECT node_id, bm25(node_fts, {weights}) AS score FROM node_fts WHERE node_fts MATCH ?"
                f" AND kind IN ({', '.join('?' for _ in kinds)}) ORDER BY score, node_id LIMIT ?",
                (fts_query, *kinds, limit * 4),  # a node's passages take rows too
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return list(dict.fromkeys(node_id for node_id, _ in rows))[:limit]

    def has_trigram(self) -> bool:
        """Whether the file has a filled ``node_tri``; one written before it, or by an SQLite without the trigram
        tokenizer, has not, and search then reads words alone."""
        if self._trigram is None:
            try:
                self._trigram = self.db.execute("SELECT 1 FROM node_tri LIMIT 1").fetchone() is not None
            except sqlite3.OperationalError:
                self._trigram = False
        return self._trigram

    def _index_row(self, node_id: str) -> tuple[str, str, str]:
        """(kind, name, path) of a node's own row in the full-text index, as ``_ranked`` reads them."""
        row = self.db.execute(
            "SELECT kind, name, path FROM node_fts WHERE rowid = (SELECT fts_rowid FROM node_fts_rows"
            " WHERE node_id = ? AND aud = 0)",
            (node_id,),
        ).fetchone()
        return (row[0], str(row[1] or "").split("\n", 1)[0], row[2] or "") if row else ("", "", "")

    def _files_named(self, word: str, kinds: list[str]) -> list[tuple[str, tuple[str, str, str]]]:
        """(node id, (kind, name, path)) of the files whose file name or stem is ``word`` (``search.file_named``),
        found through the path column of the full-text index; none when ``kinds`` leaves files out."""
        if kinds and "File" not in kinds:
            return []
        try:
            found = self.db.execute(
                "SELECT node_id, kind, name, path FROM node_fts WHERE node_fts MATCH ? AND kind = 'File' LIMIT ?",
                (f"path : {text_search.phrase(word)}", text_search.RERANK_POOL),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r[0], (r[1], r[2] or "", r[3] or "")) for r in found if r[3] and text_search.file_named(r[3], word)]

    def code_files(self, words: list[str], limit: int = text_search.CODE_POOL) -> list[tuple[str, int]]:
        """(file node id, how many of ``words`` its text holds) of the files whose code or markup (``code_fts``)
        holds a word of ``words`` whole, the most words first, then the best BM25 a word gave. A word more than
        ``limit`` files hold is left out: it names a kind of file (a brand, a framework), not the one asked for.
        [] for a graph built before the index came."""
        held: dict[str, list] = {}
        for word in words:
            try:
                rows = self.db.execute(
                    "SELECT node_id, bm25(code_fts) AS score FROM code_fts WHERE code_fts MATCH ?"
                    " ORDER BY score, node_id LIMIT ?",
                    (text_search.phrase(text_search.fold(word)), limit + 1),
                ).fetchall()
            except sqlite3.OperationalError:  # no such table, or a word FTS5 cannot read
                continue
            if len(rows) > limit:
                continue
            for node_id, score in rows:
                found = held.setdefault(node_id, [0, 0.0])
                found[0] += 1
                found[1] = min(found[1], score)
        return [(nid, held[nid][0]) for nid in sorted(held, key=lambda nid: (-held[nid][0], held[nid][1], nid))]

    def body(self, node_id: str) -> str | None:
        """The text the full-text index holds of node ``node_id``'s own row (``search.stored_text``), None without
        one; a row of ``node_fts_aud`` only for a reader within its audience, as ``search`` reads them."""
        try:
            held = self.db.execute("SELECT fts_rowid, aud FROM node_fts_rows WHERE node_id = ?", (node_id,)).fetchone()
            if held is None or (held[1] and not self.reader):
                return None
            table, seen = FTS_TABLES[held[1]], FTS_SEEN if held[1] else ""
            row = self.db.execute(f"SELECT body FROM {table} WHERE rowid = ?{seen}", (held[0],)).fetchone()
        except sqlite3.OperationalError:
            return None
        return text_search.stored_text(row[0]) if row and row[0] else None

    def code_lines(self, node_id: str, words: list[str], most: int = text_search.CODE_LINES) -> list[int]:
        """The first ``most`` lines of a file's text in ``code_fts`` that hold a word of ``words`` whole, as the
        index's tokenizer reads it (case and accents aside); [] when the index does not hold the file."""
        try:
            row = self.db.execute(
                "SELECT body FROM code_fts WHERE rowid = (SELECT fts_rowid FROM code_fts_rows WHERE node_id = ?)",
                (node_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return []
        return text_search.lines_holding(row[0], words, most) if row else []

    def matched(self, ids: list[str], query: str, snippets: Set[str] = frozenset()) -> dict[str, dict]:
        """What the full-text index says of each node of ``ids`` against ``query``: ``words``, how many of the
        query's words (``search.query_words``) its name, path or body holds whole, of ``of``; and for a node of
        ``snippets``, ``snippet``: the passage of its body where those words gather (FTS5's ``snippet``, about
        ``search.SNIPPET_TOKENS`` words, white space run together). A node the index holds no row for is left out.
        The rows of ``node_fts_aud`` are read only for a reader with an audience, and only those within it, as
        ``search`` reads them."""
        words = text_search.query_words(query)
        if not ids or not words:
            return {}
        whole = [next(text_search.fts_queries([w])) for w in words]  # each word as the all-words way matches it
        marks = ", ".join("?" for _ in ids)
        rows = self.db.execute(f"SELECT node_id, fts_rowid, aud FROM node_fts_rows WHERE node_id IN ({marks})", ids)
        by_table: dict[int, dict[int, str]] = defaultdict(dict)
        for node_id, rowid, aud in rows:
            by_table[aud][rowid] = node_id
        out: dict[str, dict] = {}
        for aud, table in enumerate(FTS_TABLES):
            of_table = by_table.get(aud)
            if not of_table or (aud and not self.reader):
                continue
            seen = FTS_SEEN if aud else ""
            within = f"rowid IN ({', '.join('?' for _ in of_table)}){seen}"
            held: dict[int, int] = dict.fromkeys(of_table, 0)
            try:
                for phrase in whole:
                    for (rowid,) in self.db.execute(
                        f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND {within}", (phrase, *of_table)
                    ):
                        held[rowid] += 1
                wanted = [rowid for rowid, node_id in of_table.items() if node_id in snippets]
                passages = {}
                if wanted:
                    passages = dict(
                        self.db.execute(
                            f"SELECT rowid, snippet({table}, 4, '', '', '…', {text_search.SNIPPET_TOKENS}) FROM {table}"
                            f" WHERE {table} MATCH ? AND rowid IN ({', '.join('?' for _ in wanted)}){seen}",
                            (" OR ".join(whole), *wanted),
                        )
                    )
            except sqlite3.OperationalError:
                continue
            for rowid, node_id in of_table.items():
                out[node_id] = {"words": held[rowid], "of": len(words)}
                passage = " ".join(str(passages.get(rowid) or "").split())
                if passage:
                    out[node_id]["snippet"] = passage
        for node_id, (lines, snippet) in self._best_passages(list(out), whole, snippets).items():
            out[node_id]["lines"] = lines
            if snippet:
                out[node_id]["snippet"] = snippet
        return out

    def _best_passages(self, ids: list[str], whole: list[str], snippets) -> dict[str, tuple[str | None, str | None]]:
        """For each node of ``ids`` indexed as passages too (``search.PASSAGE_FROM``), (the lines of its passage that
        matches the words ``whole`` best by BM25, the passage's snippet when the node is one of ``snippets``). A store
        written before passages came holds none, and answers {}."""
        weights = ", ".join(str(w) for w in self.tuning.bm25)
        query = " OR ".join(whole)
        out: dict[str, tuple[str | None, str | None]] = {}
        for node_id in ids:
            keys = self.db.execute(
                "SELECT node_id, fts_rowid, aud FROM node_fts_rows WHERE node_id > ? AND node_id < ?",
                (node_id + text_search.PASSAGE_MARK, node_id + chr(ord(text_search.PASSAGE_MARK) + 1)),
            ).fetchall()
            by_table: dict[int, dict[int, str]] = defaultdict(dict)
            for key, rowid, aud in keys:
                if not aud or self.reader:
                    by_table[aud][rowid] = key
            best = None
            for aud, rows in by_table.items():
                table = FTS_TABLES[aud]
                seen = FTS_SEEN if aud else ""
                snippet = (
                    f"snippet({table}, 4, '', '', '…', {text_search.SNIPPET_TOKENS})" if node_id in snippets else "''"
                )
                try:
                    found = self.db.execute(
                        f"SELECT rowid, bm25({table}, {weights}) AS score, {snippet} FROM {table} WHERE {table} MATCH ?"
                        f" AND rowid IN ({', '.join('?' for _ in rows)}){seen} ORDER BY score, rowid LIMIT 1",
                        (query, *rows),
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue
                if found and (best is None or found[1] < best[1]):
                    best = (rows[found[0]], found[1], found[2])
            if best is not None:
                lines = text_search.passage_lines(best[0])[1]
                out[node_id] = (lines, " ".join(str(best[2] or "").split()) or None)
        return out


def tri_row(row) -> tuple:
    """A row of ``node_tri`` from one of ``node_fts`` (rowid, node id, kind, name, path): the name without the parts
    of its camelCase words ``search.index_text`` added, which substrings find, and both in ``search.tri_text``."""
    rowid, node_id, kind, name, path = row
    name = str(name or "").split("\n", 1)[0]
    return (rowid, node_id, kind, text_search.tri_text(name), text_search.tri_text(path or ""))
