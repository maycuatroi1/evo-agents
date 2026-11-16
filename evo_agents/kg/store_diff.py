"""The bitemporal diff of the graph store (``evo_agents.kg.store``): a build closes the rows that vanished or changed
(``tx_to``) and inserts the rows that are new or changed (``tx_from``), each row known by the digest of its content."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from evo_agents.kg.graph_model import EDGE_FIELDS, NODE_FIELDS, UNIT_FIELDS, graph_tables, row_content

if TYPE_CHECKING:
    from evo_agents.kg.graph_model import Graph


def diff_rows(
    db: sqlite3.Connection, table: str, key_col: str | None, b: int, rows: dict[str, dict], cols: list[str]
) -> None:
    """Close rows that vanished or changed, insert rows that are new or changed."""
    if key_col is None:
        key_expr = " || char(31) || ".join(c for c in cols if c in ("elem_id", "deriv_id", "unit_id", "span"))
    else:
        key_expr = key_col
    live = {
        k: (rowid, c)
        for rowid, k, c in db.execute(f"SELECT rowid, {key_expr}, content FROM {table} WHERE tx_to IS NULL")
    }
    new_content = {k: row_content(r) for k, r in rows.items()}
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


def diff_graph(db: sqlite3.Connection, b: int, graph: Graph) -> None:
    """Bring the units, nodes, edges, derivations and where-provenance of build b to ``graph``'s."""
    tables = graph_tables(graph)
    diff_rows(db, "units", "unit_id", b, tables["units"], UNIT_FIELDS)
    diff_rows(db, "nodes", "node_id", b, tables["nodes"], NODE_FIELDS)
    diff_rows(db, "edges", "edge_id", b, tables["edges"], EDGE_FIELDS)
    diff_rows(db, "derivations", None, b, tables["derivations"], ["elem_id", "deriv_id", "units", "needs"])
    diff_rows(db, "where_prov", None, b, tables["where_prov"], ["elem_id", "unit_id", "span"])
