"""What a read of the graph store (``evo_agents.kg.store``) leaves out: rows not live at the build it reads, and rows
whose audience is not within the store's ``reader`` (``evo_agents.kg.policy``), tested in SQL by ``aud_seen``."""

from __future__ import annotations

import sqlite3
import weakref
from typing import Protocol

from evo_agents.kg.graph_model import within

LIVE = "tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)"
SEEN = "(label_aud IS NULL OR aud_seen(label_aud))"  # the row's audience is within the store's reader
CEILING = "label_lvl <= :lvl AND label_loc <= :loc"  # the row's label is at most the (level, location) asked for
FTS_SEEN = " AND aud_seen(aud)"  # a row of node_fts_aud within the store's reader


def live(t: str) -> str:
    """LIVE for the rows of table alias ``t``."""
    return f"{t}.tx_from <= :b AND ({t}.tx_to IS NULL OR {t}.tx_to > :b)"


def seen_column(column: str) -> str:
    """SEEN for the audience in ``column``: none, or one within the store's reader."""
    return f"({column} IS NULL OR aud_seen({column}))"


def seen(t: str) -> str:
    """SEEN for the rows of table alias ``t``."""
    return seen_column(f"{t}.label_aud")


class Reader(Protocol):
    reader: frozenset[str]


def read_as(db: sqlite3.Connection, store: Reader) -> None:
    """Make ``aud_seen`` of ``db`` test an audience against ``store.reader``, whatever it is when the SQL runs."""
    # The connection holds the function, so a strong reference to the store would make a cycle: the file stays open
    # until the cycle collector runs, and Windows cannot delete or replace a file a connection holds.
    me = weakref.ref(store)

    def aud_seen(text: str | None) -> bool:
        held = me()
        assert held is not None, "the connection outlived its store"
        return within(held.reader, text)

    db.create_function("aud_seen", 1, aud_seen)
