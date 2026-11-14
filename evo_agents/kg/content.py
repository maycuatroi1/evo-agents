"""The content hash of a graph, computed from its items, nodes and edges with no store: the hash ``Store.write`` gives
the build it writes of the same graph (``Store.content_hash``), so an in-RAM snapshot (``evo_agents.kg.snapshot``)
names the build it holds the way the hub does.

Each row is the one a store table holds for the graph (``store_diff.graph_tables``, ``store_clusters.cluster_tables``,
``store_clusters.review_table``), hashed by ``store_diff.row_content``, and the rows of a table come in the order
of its key, as the store reads them. A span-less where-provenance row sorts first, as SQL puts NULL first."""

from __future__ import annotations

from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store import SCHEMA_VERSION, Graph
from evo_agents.kg.store_clusters import Clusters, cluster_tables, review_table
from evo_agents.kg.store_diff import graph_tables, row_content
from evo_agents.kg.store_fts import code_digest, search_rows, searched_digest


def _contents(rows: dict[str, dict], order) -> list[str]:
    return [row_content(r) for r in sorted(rows.values(), key=order)]


def _span(span: str | None) -> tuple[int, str]:
    return (0, "") if span is None else (1, span)


def searched(graph: Graph) -> str:
    """The digest of what the full-text index of ``graph`` holds."""
    rows, _ = search_rows(graph)
    return searched_digest(rows, code_digest(graph.code))


def content_hash(graph: Graph) -> str:
    """The content hash of a build of ``graph``, as ``Store.content_hash`` reads it from the build's rows."""
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
