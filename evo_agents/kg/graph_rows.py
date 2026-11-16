"""The rows of a build of a graph (``snapshot.StoreRows``), made in memory: what the hub keeps of a build
(``evo_agents.hub.kg_build``).

``graph_rows`` gives the rows of the graph, with its content hash (``kg.content``). ``with_asked`` puts the asked
index in them, and ``RowsVectors`` is the ``semantic.Graph`` that ``semantic.index`` writes their vectors into.
``call_reach`` counts how far the call edges of the rows reach.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, replace

from evo_agents.kg import asked, content, semantic
from evo_agents.kg import search as text_search
from evo_agents.kg.graph_model import (
    Graph,
    graph_stats,
    graph_tables,
    review_table,
    search_rows,
    top_audience_of,
    top_label_of,
)
from evo_agents.kg.health import KINDS, is_test_path
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.snapshot import (
    StoreRows,
    asked_order,
    cluster_rows,
    edge_row,
    label_pair,
    node_row,
)

UNIT_COLUMNS = ("unit_id", "item_id", "anchor", "rev", "hash", "source", "span", "label_aud", "label_lvl", "label_loc")


def graph_rows(graph: Graph, *, coverage: dict, stats: dict, built_at: str) -> StoreRows:
    """The rows of a build of ``graph`` (``StoreRows``) with ``coverage`` and ``stats``: its summary has build id 0,
    for the keeper to replace, and no file size."""
    tables = graph_tables(graph)
    _, keyed = search_rows(graph)
    hashed = content.content_hash(graph)
    stats = {**stats, **graph_stats(graph)}
    review = sorted(review_table(graph.review).values(), key=lambda r: r["fingerprint"])
    summary = {
        "ready": True,
        "build_id": 0,
        "built_at": built_at,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "units": len(graph.units),
        "derivations": len(tables["derivations"]),
        "content_hash": hashed,
        "coverage": coverage,
        "stats": stats,
    }
    return StoreRows(
        hashed,
        tuple(node_row(nid, n) for nid, n in sorted(graph.nodes.items())),
        tuple(edge_row(eid, e) for eid, e in sorted(graph.edges.items())),
        tuple(sorted(keyed)),
        {nid: text_search.fold(text) for nid, text in graph.code.items()},
        (label_pair(top_label_of(graph)), frozenset(top_audience_of(graph).get("top_audience", ()))),
        tuple(tuple(u.get(c) for c in UNIT_COLUMNS) for _, u in sorted(tables["units"].items())),
        tuple((r["elem_id"], r["unit_id"], r["span"]) for r in sorted(tables["where_prov"].values(), key=_where_order)),
        tuple(
            (r["elem_id"], r["units"], r["needs"])
            for r in sorted(tables["derivations"].values(), key=lambda r: (r["elem_id"], r["deriv_id"]))
        ),
        summary,
        clusters=cluster_rows(graph.clusters),
        review=tuple(
            {
                "fingerprint": r["fingerprint"],
                "rule": r["rule"],
                "cluster": r["cluster_id"],
                "evidence": json.loads(r["evidence"]),
                "clusters": json.loads(r["clusters"]),
                "action": r["action"],
                "detail": json.loads(r["detail"]),
            }
            for r in review
        ),
    )


def _where_order(r: dict) -> tuple:
    return r["elem_id"], r["unit_id"], r["span"] is not None, r["span"] or ""  # as SQLite orders them


def asked_nodes(rows: StoreRows) -> asked.Nodes:
    """``asked.Nodes`` of the rows: every live node by id, and the File and Document nodes without an audience by
    path."""
    paths: dict[str, list[str]] = defaultdict(list)
    for row in sorted(rows.nodes):
        if row[1] in ("File", "Document") and row[10] is None:
            path = json.loads(row[3] or "{}").get("path")
            if path:
                paths[path.strip("/")].append(row[0])
    return asked.Nodes({row[0] for row in rows.nodes}, paths)


def with_asked(rows: StoreRows, found: list[asked.Row], weight: float | None) -> tuple[StoreRows, str | None]:
    """``rows`` with the asked index ``found`` and the weight of an asked match, and the digest
    of them: None when the index holds nothing."""
    held = [[r.node_id, r.level, r.location, r.audience, r.prior, r.asked] for r in found]
    digest = None
    if held:
        digest = sha256(canonical_json(held if weight is None else {"rows": held, "weight": float(weight)}))
    return replace(rows, asked=asked_order(held), asked_weight=None if weight is None else float(weight)), digest


class RowsVectors:
    """The rows of a build as ``semantic.index`` writes their vectors (``semantic.Graph``); ``held`` is the rows with
    the vectors it holds now."""

    def __init__(self, rows: StoreRows):
        self.held = rows

    def rows(self, chars: int) -> list[tuple[str, str, str, str | None, str]]:
        out = []
        for key, (nid, kind, name, path, body, *aud) in self.held.keyed:
            if (body or "").strip(" "):
                out.append((key, nid, kind, aud[0] if aud else None, semantic.embed_text(name, path, body, chars)))
        return sorted(out)

    def info(self) -> semantic.Info | None:
        return semantic.Info(**self.held.vector_info) if self.held.vector_info and self.held.vectors else None

    def set_mode(self, mode: str) -> None:
        if self.held.vector_info is not None:
            self.held = replace(self.held, vector_info={**self.held.vector_info, "mode": mode})

    def write(self, rows: list[tuple], meta: dict[str, str]) -> None:
        found = semantic.info_of(meta)
        vectors = tuple(sorted((key, nid, kind, aud, bytes(vec)) for key, nid, kind, aud, _, vec in rows))
        self.held = replace(self.held, vectors=vectors, vector_info=asdict(found) if found else None)

    def drop(self) -> None:
        self.held = replace(self.held, vectors=(), vector_info=None)


def call_reach(rows: StoreRows) -> dict:
    """The call reach of the rows: the Python symbols of kinds KINDS outside tests, by kind, how many no
    ``calls`` edge leads into, and the share of them."""
    called = {row[3] for row in rows.edges if row[2] == "calls"}
    total, uncalled = Counter(), Counter()
    for row in rows.nodes:
        if row[1] != "Symbol":
            continue
        props = json.loads(row[3] or "{}")
        kind, path = props.get("kind"), props.get("path")
        if kind in KINDS and isinstance(path, str) and path.lower().endswith(".py") and not is_test_path(path):
            total[kind] += 1
            uncalled[kind] += 0 if row[0] in called else 1
    out = {}
    for kind in KINDS:
        out[f"{kind}s"] = total[kind]
        out[f"uncalled_{kind}s"] = uncalled[kind]
        out[f"uncalled_{kind}_ratio"] = round(uncalled[kind] / total[kind], 4) if total[kind] else 0.0
    return out
