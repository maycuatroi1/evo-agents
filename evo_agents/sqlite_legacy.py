"""Reads the SQLite files the knowledge graph kept before the hub held it, once, to move what they hold into the
files that replaced them (plan kg-centralized-on-hub). Only reads, and only through a read-only connection; nothing
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
