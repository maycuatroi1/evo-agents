"""The vectors of a graph file and the file cache of vectors (``evo_agents.kg.semantic``): the SQLite half of the
ranking by meaning, which goes with the graph store.

The vectors sit in the graph file, in two tables outside the store's schema version and its content hash, found by
existence like the asked index: ``node_vec`` (one row a full-text row, keyed as ``node_fts_rows``, with its node, its
kind, its audience and its vector as float16) and ``node_vec_meta`` (the model, the dimension, the mode, the digest).
A reader of before ignores them. ``FileCache`` keeps vectors by model and digest of their text in ``vectors.sqlite``
next to a graph, for a build on this machine; the hub's builds keep them in its database instead
(``evo_agents.hub.kg_semantic.HubCache``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from evo_agents.kg import semantic

CACHE_FILE = "vectors.sqlite"
VEC_TABLES = ("node_vec", "node_vec_meta")
VEC_SCHEMA = """
CREATE TABLE node_vec (
    key TEXT PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL, aud TEXT, digest TEXT NOT NULL, vec BLOB NOT NULL
) STRICT;
CREATE TABLE node_vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
"""


def index_rows(
    db: sqlite3.Connection, chars: int = semantic.EMBED_CHARS
) -> list[tuple[str, str, str, str | None, str]]:
    """(key, node id, kind, audience, text) of every full-text row with a body, by key."""
    out = []
    for table, aud in (("node_fts", "NULL"), ("node_fts_aud", "f.aud")):
        flag = 0 if table == "node_fts" else 1
        out += db.execute(
            f"SELECT r.node_id, f.node_id, f.kind, {aud}, f.name, f.path, f.body FROM node_fts_rows r"
            f" JOIN {table} f ON f.rowid = r.fts_rowid WHERE r.aud = ? AND length(trim(f.body)) > 0",
            (flag,),
        ).fetchall()
    return sorted(
        (key, nid, kind, aud, semantic.embed_text(name, path, body, chars))
        for key, nid, kind, aud, name, path, body in out
    )


def has_vectors(db: sqlite3.Connection) -> bool:
    marks = ", ".join("?" for _ in VEC_TABLES)
    found = db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({marks})", VEC_TABLES).fetchone()[0]
    return found == len(VEC_TABLES)


def drop(db: sqlite3.Connection) -> None:
    for table in VEC_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.commit()


def info(db: sqlite3.Connection) -> semantic.Info | None:
    """What the graph's vectors are, None when it has none."""
    if not has_vectors(db):
        return None
    return semantic.info_of(dict(db.execute("SELECT key, value FROM node_vec_meta").fetchall()))


def vector_rows(db: sqlite3.Connection) -> list[tuple]:
    """(node id, kind, audience, vector) of every row of node_vec, by key: what ``semantic.matrix`` loads."""
    return db.execute("SELECT node_id, kind, aud, vec FROM node_vec ORDER BY key").fetchall()


def cached(db: sqlite3.Connection, model: str) -> dict[str, bytes]:
    """The vectors of the graph by digest of their text when ``model`` made them, else none: what a cache that lost
    them can take back from the graph that holds them."""
    found = info(db)
    if found is None or found.model != model:
        return {}
    return dict(db.execute("SELECT digest, vec FROM node_vec").fetchall())


class GraphFile:
    """A graph file as ``semantic.index`` writes its vectors (``semantic.Graph``)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def rows(self, chars: int) -> list[tuple[str, str, str, str | None, str]]:
        return index_rows(self.db, chars)

    def info(self) -> semantic.Info | None:
        return info(self.db)

    def set_mode(self, mode: str) -> None:
        self.db.execute("UPDATE node_vec_meta SET value = ? WHERE key = 'mode'", (mode,))
        self.db.commit()

    def write(self, rows: list[tuple], meta: dict[str, str]) -> None:
        drop(self.db)
        self.db.executescript(VEC_SCHEMA)
        self.db.executemany("INSERT INTO node_vec VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.db.executemany("INSERT INTO node_vec_meta VALUES (?, ?)", sorted(meta.items()))
        self.db.commit()

    def drop(self) -> None:
        drop(self.db)


class FileCache:
    """Vectors by (model, digest of the text), in ``vectors.sqlite`` next to a graph (``semantic.Cache``)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vec (model TEXT NOT NULL, digest TEXT NOT NULL, vec BLOB NOT NULL,"
            " PRIMARY KEY (model, digest)) STRICT, WITHOUT ROWID"
        )

    def get(self, model: str, digests: list[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for i in range(0, len(digests), 500):
            chunk = digests[i : i + 500]
            marks = ", ".join("?" for _ in chunk)
            sql = f"SELECT digest, vec FROM vec WHERE model = ? AND digest IN ({marks})"
            out.update(self.db.execute(sql, (model, *chunk)).fetchall())
        return out

    def all(self, model: str) -> dict[str, bytes]:
        """Every vector of ``model``."""
        return dict(self.db.execute("SELECT digest, vec FROM vec WHERE model = ?", (model,)).fetchall())

    def put(self, model: str, rows: Iterable[tuple[str, bytes]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?, ?)", [(model, d, v) for d, v in rows])
        self.db.commit()

    def keep(self, model: str, digests: set[str]) -> None:
        held = [d for (d,) in self.db.execute("SELECT digest FROM vec WHERE model = ?", (model,))]
        self.db.executemany(
            "DELETE FROM vec WHERE model = ? AND digest = ?", [(model, d) for d in held if d not in digests]
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def index_file(
    path: Path,
    settings: semantic.Settings,
    cache_path: Path | None = None,
    *,
    cache: semantic.Cache | None = None,
    leave_wal: bool = False,
    **kw,
) -> semantic.Indexed:
    """``semantic.index`` on the graph file at ``path``, with ``cache``, else a FileCache at ``cache_path``, else one
    next to the file; ``leave_wal`` puts the file out of WAL mode again after, as the hub keeps its graphs."""
    db = sqlite3.connect(path)
    own = None if cache is not None else FileCache(cache_path or path.with_name(CACHE_FILE))
    try:
        found = semantic.index(GraphFile(db), settings, cache if cache is not None else own, **kw)
        if leave_wal:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        return found
    finally:
        if own is not None:
            own.close()
        db.close()


def cached_in_file(path: Path, model: str) -> dict[str, bytes]:
    """``cached`` of the graph file at ``path``; none when there is no such file."""
    if not path.exists():
        return {}
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return cached(db, model)
    except sqlite3.DatabaseError:
        return {}
    finally:
        db.close()


def cache_file_vectors(path: Path, model: str) -> dict[str, bytes]:
    """Every vector of ``model`` a ``vectors.sqlite`` at ``path`` holds; none when there is no such file."""
    if not path.exists():
        return {}
    cache = FileCache(path)
    try:
        return cache.all(model)
    except sqlite3.DatabaseError:
        return {}
    finally:
        cache.close()
