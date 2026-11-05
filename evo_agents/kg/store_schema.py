"""The tables of the graph store (``evo_agents.kg.store``): the DDL every store file is made with, the tables made apart
from it, and the names of the tables a file written before them lacks."""

from __future__ import annotations

import sqlite3

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
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, props TEXT, status TEXT, conf REAL,
    label_lvl INTEGER, label_loc INTEGER, label_int TEXT, aliases TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, props TEXT, status TEXT,
    conf REAL, label_lvl INTEGER, label_loc INTEGER, label_int TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
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
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name, tx_from);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, tx_from);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, tx_from);
CREATE INDEX IF NOT EXISTS edges_id ON edges(edge_id, tx_from);
CREATE INDEX IF NOT EXISTS deriv_elem ON derivations(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS where_elem ON where_prov(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS units_id ON units(unit_id, tx_from);
CREATE INDEX IF NOT EXISTS units_item ON units(item_id, tx_from);
CREATE TABLE IF NOT EXISTS node_alias (alias TEXT PRIMARY KEY, node_id TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED, kind UNINDEXED, name, path, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts_aud USING fts5(
    node_id UNINDEXED, kind UNINDEXED, name, path, body, aud UNINDEXED, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS node_fts_rows (
    node_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL, digest TEXT NOT NULL, aud INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    node_id UNINDEXED, body, tokenize = "unicode61 remove_diacritics 2 tokenchars '_-$'"
);
CREATE TABLE IF NOT EXISTS code_fts_rows (
    node_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL, digest TEXT NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT NOT NULL, level INTEGER NOT NULL, parent TEXT, label TEXT NOT NULL, size INTEGER NOT NULL,
    kinds TEXT NOT NULL, content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS cluster_members (
    node_id TEXT NOT NULL, cluster_id TEXT NOT NULL, sub_id TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS cluster_links (
    link_id TEXT NOT NULL, level INTEGER NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, weight REAL NOT NULL,
    content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS clusters_id ON clusters(cluster_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_members_node ON cluster_members(node_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_members_cluster ON cluster_members(cluster_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_links_id ON cluster_links(link_id, tx_from);
CREATE TABLE IF NOT EXISTS review_items (
    fingerprint TEXT NOT NULL, rule TEXT NOT NULL, cluster_id TEXT, evidence TEXT NOT NULL, clusters TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL, content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS review_items_id ON review_items(fingerprint, tx_from);
CREATE TABLE IF NOT EXISTS cluster_warm (
    build_id INTEGER NOT NULL, node_id TEXT NOT NULL, cluster_id TEXT NOT NULL, sub_id TEXT,
    PRIMARY KEY (build_id, node_id)
) STRICT, WITHOUT ROWID;
"""
ASKED_TABLES = ("node_asked", "node_asked_rows")  # what agents asked before they read a node; only in a hub build
ASKED_SCHEMA = """
CREATE VIRTUAL TABLE node_asked USING fts5(asked, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE node_asked_rows (
    id INTEGER PRIMARY KEY, node_id TEXT NOT NULL, lvl INTEGER NOT NULL, loc INTEGER NOT NULL, aud TEXT,
    prior REAL NOT NULL
) STRICT;
CREATE INDEX node_asked_node ON node_asked_rows(node_id);
"""
FTS_TABLES = ("node_fts", "node_fts_aud")  # the full-text rows of nodes without an audience, and with one
# The name and path of each own row of node_fts (no passage, no audience) again, under its rowid, cut in trigrams
# (``search.tri_text``), so a word of a query matches inside a word of them. Made apart from SCHEMA: an SQLite without
# the trigram tokenizer (before 3.34) still opens the store, without it.
TRIGRAM_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS node_tri USING fts5("
    "node_id UNINDEXED, kind UNINDEXED, name, path, tokenize = 'trigram')"
)
OWN_ROWS = "node_fts_rows WHERE aud = 0 AND instr(node_id, char(31)) = 0"  # the rows of node_fts that node_tri holds
NODE_COLUMNS = "node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases, label_aud"
EDGE_COLUMNS = "edge_id, src, rel, dst, props, status, conf, label_lvl, label_loc, label_int, label_aud"
CLUSTER_TABLES = ("clusters", "cluster_members", "cluster_links")  # absent from a file written before clusters came
REVIEW_TABLES = ("review_items",)  # absent from a file written before the review queue came


def schema_version(db: sqlite3.Connection) -> str | None:
    """The schema version the file of ``db`` records; None when it records none."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


def has_tables(db: sqlite3.Connection, tables: tuple[str, ...]) -> bool:
    """Whether the file of ``db`` has every table of ``tables``."""
    marks = ", ".join("?" for _ in tables)
    sql = f"SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN ({marks})"
    return db.execute(sql, tables).fetchone()[0] == len(tables)
