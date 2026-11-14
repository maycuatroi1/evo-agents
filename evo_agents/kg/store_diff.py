"""The bitemporal diff of the graph store (``evo_agents.kg.store``): a build closes the rows that vanished or changed
(``tx_to``) and inserts the rows that are new or changed (``tx_from``), each row known by the digest of its content."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from evo_agents.kg.policy import audience_join
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store_filters import label_columns, stored_audience

if TYPE_CHECKING:
    from evo_agents.kg.store import Graph

UNIT_FIELDS = [
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
    "label_aud",
]
NODE_FIELDS = [
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
    "label_aud",
]
EDGE_FIELDS = ["edge_id", "src", "rel", "dst", "props", "status", "conf"] + [
    "label_lvl",
    "label_loc",
    "label_int",
    "label_aud",
]


def row_content(row: dict) -> str:
    return sha256(canonical_json(row))


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


def graph_tables(graph: Graph) -> dict[str, dict[str, dict]]:
    """The rows ``graph`` puts in each graph table, by key: what ``diff_graph`` writes and ``kg.content`` hashes."""
    units = {uid: {"unit_id": uid, **u} for uid, u in graph.units.items()}
    derivs = {}
    for elem, ds in graph.derivations.items():
        for d in ds:
            unit_ids, needs = json.dumps(sorted(d["units"])), json.dumps(sorted(d["needs"]))
            did = sha256(f"{elem}\x1f{unit_ids}\x1f{needs}")[7:23]
            derivs[f"{elem}\x1f{did}"] = {"elem_id": elem, "deriv_id": did, "units": unit_ids, "needs": needs}
    wheres = {}
    for elem, entries in graph.where.items():
        for unit, span in entries:
            wheres[f"{elem}\x1f{unit}\x1f{span}"] = {"elem_id": elem, "unit_id": unit, "span": span}
    return {
        "units": units,
        "nodes": {nid: node_columns(nid, n) for nid, n in graph.nodes.items()},
        "edges": {eid: edge_columns(eid, e) for eid, e in graph.edges.items()},
        "derivations": derivs,
        "where_prov": wheres,
    }


def node_columns(nid: str, n: dict) -> dict:
    return {
        "node_id": nid,
        "kind": n["kind"],
        "name": n["name"],
        "props": json.dumps(n["props"], ensure_ascii=False, sort_keys=True),
        "status": n["status"],
        "conf": n["conf"],
        **label_columns(n["label"]),
        "aliases": json.dumps(n.get("aliases", [])),
    }


def edge_columns(eid: str, e: dict) -> dict:
    return {
        "edge_id": eid,
        "src": e["src"],
        "rel": e["rel"],
        "dst": e["dst"],
        "props": json.dumps(e["props"], ensure_ascii=False, sort_keys=True),
        "status": e["status"],
        "conf": e["conf"],
        **label_columns(e["label"]),
    }


def graph_stats(graph: Graph) -> dict:
    """The ``size``, ``top_label`` and ``top_audience`` a build of ``graph`` records in its ``stats``."""
    return {"size": size_of(graph), "top_label": top_label_of(graph), **top_audience_of(graph)}


def size_of(graph: Graph) -> dict:
    return {"nodes": len(graph.nodes), "edges": len(graph.edges), "units": len(graph.units)}


def top_label_of(graph: Graph) -> list[int]:
    """The highest level and the highest location among the labels of ``graph``'s nodes, edges and units."""
    pairs = [(e["label"].level, e["label"].location) for e in (*graph.nodes.values(), *graph.edges.values())]
    pairs += [(u["label_lvl"], u["label_loc"]) for u in graph.units.values()]
    return [max((p[0] for p in pairs), default=0), max((p[1] for p in pairs), default=0)]


def top_audience_of(graph: Graph) -> dict:
    """``top_audience``, the join of the audiences of ``graph``'s nodes, edges and units, when one of them has one."""
    found = frozenset()
    for audience in [e["label"].audience for e in (*graph.nodes.values(), *graph.edges.values())] + [
        stored_audience(u.get("label_aud")) for u in graph.units.values()
    ]:
        found = audience_join(found, audience)
    return {"top_audience": sorted(found)} if found else {}
