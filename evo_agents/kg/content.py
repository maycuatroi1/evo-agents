"""The content hash of a graph, computed from its items, nodes and edges: the hash of the rows of its build
(``evo_agents.kg.graph_rows``), so an in-RAM snapshot (``evo_agents.kg.snapshot``) names the build it holds the way
the hub does.

Each row is the one a table of the build holds for the graph (``graph_model.graph_tables``,
``graph_model.cluster_tables``, ``graph_model.review_table``), hashed by ``graph_model.row_content``, and the rows of a
table come in the order of its key. A span-less where-provenance row sorts first, as SQL puts NULL first."""

from __future__ import annotations

from evo_agents.kg.graph_model import (
    SCHEMA_VERSION,
    Clusters,
    Graph,
    cluster_tables,
    code_digest,
    graph_tables,
    review_table,
    row_content,
    search_rows,
    searched_digest,
)
from evo_agents.kg.protocol import canonical_json, sha256


def _contents(rows: dict[str, dict], order) -> list[str]:
    return [row_content(r) for r in sorted(rows.values(), key=order)]


def _span(span: str | None) -> tuple[int, str]:
    return (0, "") if span is None else (1, span)


def searched(graph: Graph) -> str:
    """The digest of what the full-text index of ``graph`` holds."""
    rows, _ = search_rows(graph)
    return searched_digest(rows, code_digest(graph.code))


def content_hash(graph: Graph) -> str:
    """The content hash of a build of ``graph``."""
    tables = graph_tables(graph)
    parts: list = [
        SCHEMA_VERSION,
        searched(graph),
        _contents(tables["units"], lambda r: r["unit_id"]),
        _contents(tables["nodes"], lambda r: r["node_id"]),
        _contents(tables["edges"], lambda r: r["edge_id"]),
        _contents(tables["derivations"], lambda r: (r["elem_id"], r["deriv_id"])),
        _contents(tables["where_prov"], lambda r: (r["elem_id"], r["unit_id"], _span(r["span"]))),
    ]
    clusters = cluster_tables(graph.clusters or Clusters())
    clustered = [
        _contents(clusters["clusters"], lambda r: r["cluster_id"]),
        _contents(clusters["cluster_members"], lambda r: r["node_id"]),
        _contents(clusters["cluster_links"], lambda r: r["link_id"]),
    ]
    if any(clustered):
        parts.append(clustered)
    reviewed = _contents(review_table(graph.review), lambda r: r["fingerprint"])
    if reviewed:
        parts.append({"review": reviewed})
    return sha256(canonical_json(parts))
