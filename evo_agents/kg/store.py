"""The project graph store (definition D7): one SQLite file, rebuildable from the log.

Every row carries ``tx_from`` and ``tx_to`` (build ids). A build writes only what changed: rows that
disappeared or changed get ``tx_to``, new versions get ``tx_from``; then the build flips to ``ready``.
Readers pin the latest ready build B and see rows with ``tx_from <= B < tx_to``, so a build in progress
is never visible, and retracting a fact keeps its history queryable.

Provenance is stored as a circuit: each element has derivations, and a derivation is the AND of its
ownership units and of the elements it needs. An element is alive while one derivation holds (OR).

Keys (PG-Keys): a node of a keyed kind carries ``props.key``, and no two live, non-proposed nodes of that
kind share one. A Symbol's key is ``[repo, path, qualname]``, so two sources reading the same file of the
same repo cannot both put the same function in the graph.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.ids import utc_now
from evo_agents.kg.protocol import canonical_json, sha256

SCHEMA_VERSION = "2"
KEYED_KINDS = ("Symbol",)  # kinds whose props.key is unique; keep in step with the nodes_key index

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS builds (
    build_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state_digest TEXT,
    content_hash TEXT,
    coverage TEXT,
    stats TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT NOT NULL, item_id TEXT NOT NULL, anchor TEXT NOT NULL, rev TEXT, hash TEXT, source TEXT,
    span TEXT, label_lvl INTEGER, label_loc INTEGER, label_int TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, props TEXT, status TEXT, conf REAL,
    label_lvl INTEGER, label_loc INTEGER, label_int TEXT, aliases TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, props TEXT, status TEXT,
    conf REAL, label_lvl INTEGER, label_loc INTEGER, label_int TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS derivations (
    elem_id TEXT NOT NULL, deriv_id TEXT NOT NULL, units TEXT NOT NULL, needs TEXT NOT NULL,
    content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS where_prov (
    elem_id TEXT NOT NULL, unit_id TEXT NOT NULL, span TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS nodes_live ON nodes(node_id) WHERE tx_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS nodes_key ON nodes(kind, json_extract(props, '$.key'))
    WHERE kind = 'Symbol' AND tx_to IS NULL AND status <> 'proposed';
CREATE INDEX IF NOT EXISTS nodes_tx ON nodes(node_id, tx_from);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, tx_from);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, tx_from);
CREATE INDEX IF NOT EXISTS edges_id ON edges(edge_id, tx_from);
CREATE INDEX IF NOT EXISTS deriv_elem ON derivations(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS where_elem ON where_prov(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS units_id ON units(unit_id, tx_from);
CREATE INDEX IF NOT EXISTS units_item ON units(item_id, tx_from);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED, name, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""

LIVE = "tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)"


def edge_id(src: str, rel: str, dst: str) -> str:
    return "e:" + sha256(f"{src}\x1f{rel}\x1f{dst}")[7:23]


@dataclass
class Graph:
    """An assembled graph, ready to write: plain dicts keyed by id."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    derivations: dict[str, list[dict]] = field(default_factory=dict)  # elem id -> [{units, needs}]
    where: dict[str, list[list[str]]] = field(default_factory=dict)  # elem id -> [[unit, span]]
    units: dict[str, dict] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)  # node id -> searchable text


def _content(row: dict) -> str:
    return sha256(canonical_json(row))


def key_conflicts(graph: Graph) -> dict[tuple[str, str], list[str]]:
    """Keys that more than one node of the graph would hold, under the same predicate as the nodes_key
    index. Checked before writing, so a violation names its nodes instead of surfacing as an IntegrityError."""
    holders: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid, n in graph.nodes.items():
        key = (n.get("props") or {}).get("key")
        if n["kind"] in KEYED_KINDS and n["status"] not in (None, "proposed") and key is not None:
            holders[(n["kind"], json.dumps(key, ensure_ascii=False))].append(nid)
    return {k: sorted(ids) for k, ids in sorted(holders.items()) if len(ids) > 1}


class StaleSchema(RuntimeError):
    """A store file written by an older schema. ``kg build`` rebuilds it from the log; readers refuse it."""

    def __init__(self, path: Path, found: str):
        self.path, self.found = path, found
        super().__init__(
            f"{path} has store schema {found}, this code reads {SCHEMA_VERSION}:"
            " run `evo-agents kg build` to rebuild it from the corpus log"
        )


def _schema_version(db: sqlite3.Connection) -> str | None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        found = _schema_version(self.db)
        if found is not None and found != SCHEMA_VERSION:
            self.db.close()
            raise StaleSchema(path, found)  # before SCHEMA runs: an old file is never half-migrated
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    @classmethod
    def for_project(cls, project) -> Store:
        return cls(project.root / "graph.sqlite")

    @classmethod
    def open_existing(cls, project) -> Store | None:
        """The project's store for reading; raises StaleSchema on a file from an older schema."""
        path = project.root / "graph.sqlite"
        return cls(path) if path.exists() else None

    @staticmethod
    def stored_schema(path: Path) -> str | None:
        """The schema version a store file was written with, without migrating it; None if there is none."""
        if not path.exists():
            return None
        db = sqlite3.connect(path)
        try:
            return _schema_version(db)
        finally:
            db.close()

    @classmethod
    def staging(cls, path: Path) -> Store:
        """An empty store next to ``path``, to be filled and then moved over it with ``install``."""
        stage = path.with_name(path.name + ".rebuild")
        for leftover in (stage, Path(f"{stage}-wal"), Path(f"{stage}-shm")):
            leftover.unlink(missing_ok=True)
        return cls(stage)

    def install(self, target: Path) -> Store:
        """Replace ``target`` with this store's file in one rename and return a store opened there. The
        file leaves WAL mode first, and the old file's WAL and shared memory go, so the new file never
        meets a log written for the old one."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.close()
        for stale in (Path(f"{target}-wal"), Path(f"{target}-shm")):
            stale.unlink(missing_ok=True)
        os.replace(self.path, target)
        return Store(target)

    def close(self) -> None:
        self.db.close()

    # -- builds ---------------------------------------------------------------------------------

    def latest_ready(self) -> int | None:
        row = self.db.execute("SELECT max(build_id) FROM builds WHERE status = 'ready'").fetchone()
        return row[0] if row and row[0] is not None else None

    def build_row(self, build_id: int) -> dict:
        row = self.db.execute(
            "SELECT build_id, status, started_at, finished_at, state_digest, content_hash, coverage, stats"
            " FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        keys = ("build_id", "status", "started_at", "finished_at", "state_digest", "content_hash", "coverage", "stats")
        out = dict(zip(keys, row, strict=True))
        out["coverage"] = json.loads(out["coverage"] or "{}")
        out["stats"] = json.loads(out["stats"] or "{}")
        return out

    def write(self, graph: Graph, *, state_digest: str, coverage: dict, stats: dict, policy) -> int:
        """Write a graph as a new build, diffing against the previous one, in one transaction."""
        db = self.db
        with db:
            cur = db.execute(
                "INSERT INTO builds (status, started_at, state_digest) VALUES ('building', ?, ?)",
                (utc_now(), state_digest),
            )
            b = cur.lastrowid
            self._diff(
                "units",
                "unit_id",
                b,
                {uid: {"unit_id": uid, **u} for uid, u in graph.units.items()},
                [
                    "unit_id",
                    "item_id",
                    "anchor",
                    "rev",
                    "hash",
                    "source",
                    "span",
                    "label_lvl",
                    "label_loc",
                    "label_int",
                ],
            )
            self._diff(
                "nodes",
                "node_id",
                b,
                {
                    nid: {
                        "node_id": nid,
                        "kind": n["kind"],
                        "name": n["name"],
                        "props": json.dumps(n["props"], ensure_ascii=False, sort_keys=True),
                        "status": n["status"],
                        "conf": n["conf"],
                        **self._label(n["label"]),
                        "aliases": json.dumps(n.get("aliases", [])),
                    }
                    for nid, n in graph.nodes.items()
                },
                [
                    "node_id",
                    "kind",
                    "name",
                    "props",
                    "status",
                    "conf",
                    "label_lvl",
                    "label_loc",
                    "label_int",
                    "aliases",
                ],
            )
            self._diff(
                "edges",
                "edge_id",
                b,
                {
                    eid: {
                        "edge_id": eid,
                        "src": e["src"],
                        "rel": e["rel"],
                        "dst": e["dst"],
                        "props": json.dumps(e["props"], ensure_ascii=False, sort_keys=True),
                        "status": e["status"],
                        "conf": e["conf"],
                        **self._label(e["label"]),
                    }
                    for eid, e in graph.edges.items()
                },
                ["edge_id", "src", "rel", "dst", "props", "status", "conf", "label_lvl", "label_loc", "label_int"],
            )
            derivs = {}
            for elem, ds in graph.derivations.items():
                for d in ds:
                    units, needs = json.dumps(sorted(d["units"])), json.dumps(sorted(d["needs"]))
                    did = sha256(f"{elem}\x1f{units}\x1f{needs}")[7:23]
                    derivs[f"{elem}\x1f{did}"] = {"elem_id": elem, "deriv_id": did, "units": units, "needs": needs}
            self._diff("derivations", None, b, derivs, ["elem_id", "deriv_id", "units", "needs"])
            wheres = {}
            for elem, entries in graph.where.items():
                for unit, span in entries:
                    wheres[f"{elem}\x1f{unit}\x1f{span}"] = {"elem_id": elem, "unit_id": unit, "span": span}
            self._diff("where_prov", None, b, wheres, ["elem_id", "unit_id", "span"])
            db.execute("DELETE FROM node_fts")
            db.executemany(
                "INSERT INTO node_fts (node_id, name, body) VALUES (?, ?, ?)",
                [(nid, n["name"] or "", graph.texts.get(nid, "")[:4000]) for nid, n in sorted(graph.nodes.items())],
            )
            content_hash = self.content_hash(b)
            db.execute(
                "UPDATE builds SET status = 'ready', finished_at = ?, content_hash = ?, coverage = ?, stats = ?"
                " WHERE build_id = ?",
                (utc_now(), content_hash, json.dumps(coverage, ensure_ascii=False), json.dumps(stats), b),
            )
        return b

    @staticmethod
    def _label(label) -> dict:
        return {"label_lvl": label.level, "label_loc": label.location, "label_int": label.integrity}

    def _diff(self, table: str, key_col: str | None, b: int, rows: dict[str, dict], cols: list[str]) -> None:
        """Close rows that vanished or changed, insert rows that are new or changed."""
        db = self.db
        if key_col is None:
            key_expr = " || char(31) || ".join(c for c in cols if c in ("elem_id", "deriv_id", "unit_id", "span"))
        else:
            key_expr = key_col
        live = {
            k: (rowid, c)
            for rowid, k, c in db.execute(f"SELECT rowid, {key_expr}, content FROM {table} WHERE tx_to IS NULL")
        }
        new_content = {k: _content(r) for k, r in rows.items()}
        gone = [rowid for k, (rowid, c) in live.items() if new_content.get(k) != c]
        db.executemany(f"UPDATE {table} SET tx_to = ? WHERE rowid = ?", [(b, rowid) for rowid in gone])
        placeholders = ", ".join("?" for _ in cols + ["content", "tx_from"])
        db.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}, content, tx_from) VALUES ({placeholders})",
            [
                tuple(r.get(c) for c in cols) + (new_content[k], b)
                for k, r in rows.items()
                if live.get(k, (None, None))[1] != new_content[k]
            ],
        )

    # -- reading --------------------------------------------------------------------------------

    def content_hash(self, b: int) -> str:
        """Hash of everything alive at build b, independent of how the build got there."""
        parts = []
        for table, order in (
            ("units", "unit_id"),
            ("nodes", "node_id"),
            ("edges", "edge_id"),
            ("derivations", "elem_id, deriv_id"),
            ("where_prov", "elem_id, unit_id, span"),
        ):
            rows = self.db.execute(f"SELECT content FROM {table} WHERE {LIVE} ORDER BY {order}", {"b": b})
            parts.append([r[0] for r in rows])
        return sha256(canonical_json(parts))

    def counts(self, b: int) -> dict:
        out = {}
        for table in ("nodes", "edges", "units", "derivations", "where_prov"):
            out[table] = self.db.execute(f"SELECT count(*) FROM {table} WHERE {LIVE}", {"b": b}).fetchone()[0]
        return out

    def summary(self) -> dict:
        b = self.latest_ready()
        if b is None:
            return {"ready": False}
        row = self.build_row(b)
        counts = self.counts(b)
        return {
            "ready": True,
            "build_id": b,
            "built_at": row["finished_at"],
            "nodes": counts["nodes"],
            "edges": counts["edges"],
            "units": counts["units"],
            "derivations": counts["derivations"],
            "content_hash": row["content_hash"],
            "coverage": row["coverage"],
            "stats": row["stats"],
            "file_bytes": self.path.stat().st_size,
        }

    def node(self, node_id: str, b: int) -> dict | None:
        row = self.db.execute(
            f"SELECT node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases FROM nodes"
            f" WHERE node_id = :id AND {LIVE}",
            {"id": node_id, "b": b},
        ).fetchone()
        return _node_row(row) if row else None

    def nodes(self, ids, b: int) -> dict[str, dict]:
        out = {}
        ids = list(ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self.db.execute(
                f"SELECT node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases"
                f" FROM nodes WHERE node_id IN ({marks}) AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)",
                (*chunk, b, b),
            ):
                out[row[0]] = _node_row(row)
        return out

    def edges_of(self, node_id: str, b: int, direction: str = "both", limit: int = 200) -> list[dict]:
        clauses = {"out": "src = :id", "in": "dst = :id", "both": "(src = :id OR dst = :id)"}[direction]
        rows = self.db.execute(
            f"SELECT edge_id, src, rel, dst, props, status, conf, label_lvl, label_loc, label_int FROM edges"
            f" WHERE {clauses} AND {LIVE} ORDER BY rel, src, dst LIMIT :lim",
            {"id": node_id, "b": b, "lim": limit},
        )
        return [_edge_row(r) for r in rows]

    def degree(self, node_id: str, b: int) -> int:
        return self.db.execute(
            f"SELECT count(*) FROM edges WHERE (src = :id OR dst = :id) AND {LIVE}", {"id": node_id, "b": b}
        ).fetchone()[0]

    def search(self, query: str, b: int, limit: int = 50) -> list[str]:
        """Node ids matching a query: exact id or name first, then full-text."""
        hits: list[str] = []
        exact = self.db.execute(
            f"SELECT node_id FROM nodes WHERE (node_id = :q OR name = :q) AND {LIVE} ORDER BY node_id LIMIT :lim",
            {"q": query, "b": b, "lim": limit},
        )
        hits.extend(r[0] for r in exact)
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
        # Whole tokens first, so "KB-01" does not drown in KB-010..KB-019; prefixes only fill the rest.
        for fts_query in (" ".join(f'"{t}"' for t in terms), " ".join(f'"{t}"*' for t in terms)):
            if not terms or len(hits) >= limit * 3:
                break
            try:
                rows = self.db.execute(
                    "SELECT node_id FROM node_fts WHERE node_fts MATCH ?"
                    " ORDER BY bm25(node_fts, 0, 5.0, 1.0), node_id LIMIT ?",
                    (fts_query, limit * 3),
                )
                for r in rows:
                    if r[0] not in hits:
                        hits.append(r[0])
            except sqlite3.OperationalError:
                pass
        return hits[: limit * 3]

    def derivations(self, elem_id: str, b: int) -> list[dict]:
        rows = self.db.execute(
            f"SELECT units, needs FROM derivations WHERE elem_id = :e AND {LIVE} ORDER BY deriv_id",
            {"e": elem_id, "b": b},
        )
        return [{"units": json.loads(r[0]), "needs": json.loads(r[1])} for r in rows]

    def where(self, elem_id: str, b: int) -> list[dict]:
        rows = self.db.execute(
            f"SELECT unit_id, span FROM where_prov WHERE elem_id = :e AND {LIVE} ORDER BY unit_id, span",
            {"e": elem_id, "b": b},
        )
        return [{"unit": r[0], "span": r[1]} for r in rows]

    def unit(self, unit_id: str, b: int) -> dict | None:
        row = self.db.execute(
            f"SELECT unit_id, item_id, anchor, rev, hash, source, span FROM units WHERE unit_id = :u AND {LIVE}",
            {"u": unit_id, "b": b},
        ).fetchone()
        keys = ("unit", "item", "anchor", "rev", "hash", "source", "span")
        return dict(zip(keys, row, strict=True)) if row else None

    def alive_ids(self, table: str, b: int) -> set[str]:
        col = {"nodes": "node_id", "edges": "edge_id", "units": "unit_id"}[table]
        return {r[0] for r in self.db.execute(f"SELECT {col} FROM {table} WHERE {LIVE}", {"b": b})}

    def predict_retraction(self, removed_units: set[str], b: int) -> set[str]:
        """Evaluate the provenance circuit with some units set to false: which elements would die."""
        derivs: dict[str, list[tuple[list, list]]] = {}
        for elem, units, needs in self.db.execute(
            f"SELECT elem_id, units, needs FROM derivations WHERE {LIVE}", {"b": b}
        ):
            derivs.setdefault(elem, []).append((json.loads(units), json.loads(needs)))
        alive = set(derivs)
        changed = True
        while changed:
            changed = False
            for elem in list(alive):
                if not any(
                    not (set(u) & removed_units) and all(n in alive for n in needs) for u, needs in derivs[elem]
                ):
                    alive.discard(elem)
                    changed = True
        return set(derivs) - alive


def _node_row(row) -> dict:
    return {
        "id": row[0],
        "kind": row[1],
        "name": row[2],
        "props": json.loads(row[3] or "{}"),
        "status": row[4],
        "conf": row[5],
        "label": (row[6], row[7], row[8]),
        "aliases": json.loads(row[9] or "[]"),
    }


def _edge_row(row) -> dict:
    return {
        "id": row[0],
        "src": row[1],
        "rel": row[2],
        "dst": row[3],
        "props": json.loads(row[4] or "{}"),
        "status": row[5],
        "conf": row[6],
        "label": (row[7], row[8], row[9]),
    }
