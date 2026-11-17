"""Reads the SQLite files the knowledge graph kept before the hub held it: once, to move what they hold into the
files and tables that replaced them, and in the graph files older builds uploaded, for the secrets the hub's scrub
looks for until those files leave the bucket (plan kg-centralized-on-hub). Only reads, and only through a read-only connection; nothing
here writes a SQLite file. It sits outside ``evo_agents.kg``, ``evo_agents.hub`` and ``evo_agents.worker``, which use
no SQLite, and goes with those old files at step 7 of the plan."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path


def memo_rows(path: Path) -> Iterator[tuple[str, str, bytes, str]]:
    """(key, stage, gzipped output, output hash) of each row of the memo.sqlite at ``path``. Raises
    ``sqlite3.Error`` when it cannot be read whole, so the caller can try again rather than take part of it."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield from db.execute("SELECT key, stage, output, output_hash FROM memo")
    finally:
        db.close()


def errors() -> tuple[type[Exception], ...]:
    """What reading an old file raises when it is not one, or not whole."""
    return (sqlite3.Error,)


def vector_cache_rows(path: Path, model: str) -> dict[str, bytes]:
    """The vectors of ``model`` by digest of their text in the ``vectors.sqlite`` at ``path``, which a build kept next
    to its graph before the hub's kg_vector_cache; none when there is no such file, or it is not one."""
    return _read(path, "SELECT digest, vec FROM vec WHERE model = ?", (model,))


def graph_vectors(path: Path, model: str) -> dict[str, bytes]:
    """The vectors of the ``graph.sqlite`` at ``path`` by digest of their text, when ``model`` made them; none
    otherwise, or when there is no such file, or it is not one."""
    meta = dict(_read(path, "SELECT key, value FROM node_vec_meta", ()))
    if meta.get("model") != model:
        return {}
    return _read(path, "SELECT digest, vec FROM node_vec", ())


def _read(path: Path, query: str, args: tuple) -> dict:
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return dict(db.execute(query, args).fetchall())
    except sqlite3.DatabaseError:  # not that file, or one without that table
        return {}
    finally:
        db.close()


GRAPH_QUERIES = (  # every row of a graph file that holds text, current or not
    "SELECT name, body FROM node_fts",
    "SELECT name, props FROM nodes",
    "SELECT props, NULL FROM edges",
)


def graph_text_rows(path: Path, *, immutable: bool = False) -> Iterator[tuple[str | None, str | None]]:
    """The pairs of text of each row of the graph file at ``path`` (``GRAPH_QUERIES``): the full-text index of the
    nodes (name and text), and the name and properties of every node and edge. A table a store of another schema lacks
    holds nothing."""
    mode = "mode=ro&immutable=1" if immutable else "mode=ro"
    db = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
    try:
        for query in GRAPH_QUERIES:
            try:
                found = db.execute(query)
            except sqlite3.OperationalError:
                continue
            yield from found
    finally:
        db.close()
