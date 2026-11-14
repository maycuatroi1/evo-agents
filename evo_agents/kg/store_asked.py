"""What agents asked before they read a node (``evo_agents.kg.asked``), in the graph store (``evo_agents.kg.store``):
``node_asked`` holds the words, full-text, and ``node_asked_rows`` each row's node, label and prior. Only a hub build
writes them, and search reads them only for a reader whose ``asked_clearance`` is set."""

from __future__ import annotations

import sqlite3

from evo_agents.kg import asked as asked_words
from evo_agents.kg import search as text_search
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store_filters import seen_column
from evo_agents.kg.store_schema import ASKED_SCHEMA, ASKED_TABLES


class AskedIndex:
    """The methods of ``Store`` that write and read the asked index."""

    db: sqlite3.Connection
    asked_clearance: tuple[int, int] | None
    asked_weight: float | None
    _asked: bool | None
    _asked_weight: tuple[float | None] | None

    def has_asked(self) -> bool:
        """Whether the file has the asked tables, looked up once until ``write_asked`` changes them."""
        found = self._asked
        if found is None:
            marks = ", ".join("?" for _ in ASKED_TABLES)
            sql = f"SELECT count(*) FROM sqlite_master WHERE name IN ({marks})"
            found = self._asked = self.db.execute(sql, ASKED_TABLES).fetchone()[0] == len(ASKED_TABLES)
        return found

    def weight_of_asked(self) -> float:
        """The weight of an asked match: ``asked_weight`` when set, else the one the build recorded in the file's
        meta (the KG optimize loop tunes it a project), else ``search.ASKED_WEIGHT``."""
        if self.asked_weight is not None:
            return self.asked_weight
        recorded = self.recorded_asked_weight()
        return text_search.ASKED_WEIGHT if recorded is None else recorded

    def recorded_asked_weight(self) -> float | None:
        """The weight of an asked match the build recorded in the file's meta, or None."""
        if self._asked_weight is None:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'asked_weight'").fetchone()
            try:
                self._asked_weight = (float(row[0]),) if row is not None else (None,)
            except (TypeError, ValueError):
                self._asked_weight = (None,)
        return self._asked_weight[0]

    def write_asked(self, rows, weight: float | None = None) -> str | None:
        """Put ``rows`` (``evo_agents.kg.asked.Row``) in place of the asked index the file had: the index is derived
        from the hub's labels at each build, never history. ``weight``, the project's tuned weight of an asked match,
        is recorded in the file's meta (None: ``search.ASKED_WEIGHT``). The digest of what it holds now, the weight
        with it when one is given; None when it holds nothing (no rows: the tables are gone, and the file searches as
        before)."""
        rows = list(rows)
        for table in ASKED_TABLES:
            self.db.execute(f"DROP TABLE IF EXISTS {table}")
        self._asked = None
        self._asked_weight = None
        self.db.execute("DELETE FROM meta WHERE key = 'asked_weight'")
        if weight is not None:
            self.db.execute("INSERT INTO meta VALUES ('asked_weight', ?)", (repr(float(weight)),))
        if not rows:
            self.db.commit()
            return None
        self.db.executescript(ASKED_SCHEMA)
        for n, row in enumerate(rows, start=1):
            self.db.execute(
                "INSERT INTO node_asked_rows VALUES (?, ?, ?, ?, ?, ?)",
                (n, row.node_id, row.level, row.location, row.audience, row.prior),
            )
            self.db.execute("INSERT INTO node_asked(rowid, asked) VALUES (?, ?)", (n, row.asked))
        self.db.commit()
        self._asked = None
        held = [[r.node_id, r.level, r.location, r.audience, r.prior, r.asked] for r in rows]
        return sha256(canonical_json(held if weight is None else {"rows": held, "weight": float(weight)}))

    @staticmethod
    def _asked_where(clearance: tuple[int, int]) -> tuple[str, tuple]:
        """The WHERE clause of the asked rows ``r`` within ``clearance`` (level, location) and the reader's audience."""
        lvl, loc = clearance
        return f"r.lvl <= ? AND r.loc <= ? AND {seen_column('r.aud')}", (lvl, loc)

    def _asked_scores(self, query: str) -> dict[str, float]:
        """Per node, how well the best of its asked rows the reader may read matches ``query``'s words, from 0 to 1
        (``evo_agents.kg.asked.match``); empty without the tables, a clearance, or ASKED_WEIGHT."""
        clearance = self.asked_clearance
        if clearance is None or not self.weight_of_asked() or not self.has_asked():
            return {}
        words = asked_words.terms(query)
        fts = asked_words.fts_query(query)
        if fts is None:
            return {}
        where, params = self._asked_where(clearance)
        try:
            found = self.db.execute(
                "SELECT r.node_id, node_asked.asked FROM node_asked JOIN node_asked_rows r ON r.id ="
                f" node_asked.rowid WHERE node_asked MATCH ? AND {where}",
                (fts, *params),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        out: dict[str, float] = {}
        for node_id, text in found:
            out[node_id] = max(out.get(node_id, 0.0), asked_words.match(words, text))
        return out

    def _asked_priors(self, ids: list[str]) -> dict[str, float]:
        """Per node of ``ids``, the highest prior of its asked rows the reader may read, at most PRIOR_MAX."""
        clearance = self.asked_clearance
        if clearance is None or not ids or not self.has_asked():
            return {}
        where, params = self._asked_where(clearance)
        out: dict[str, float] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for node_id, prior in self.db.execute(
                f"SELECT r.node_id, max(r.prior) FROM node_asked_rows r WHERE r.node_id IN ({marks}) AND {where}"
                " GROUP BY r.node_id",
                (*chunk, *params),
            ):
                out[node_id] = min(max(prior, 1.0), asked_words.PRIOR_MAX)
        return out

    def asked_nodes(self, ids) -> set[str]:
        """The nodes of ``ids`` with an asked row under any label: some agent read them after a search."""
        ids = list(ids)
        if not ids or not self.has_asked():
            return set()
        marks = ", ".join("?" for _ in ids)
        sql = f"SELECT DISTINCT node_id FROM node_asked_rows WHERE node_id IN ({marks})"
        return {row[0] for row in self.db.execute(sql, ids)}
