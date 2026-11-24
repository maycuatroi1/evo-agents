"""The rows a snapshot (``evo_agents.kg.snapshot``) is made of, one build's (``StoreRows``), and the orders and
readings of its asked rows, clusters and vectors."""

from __future__ import annotations

import json
import struct
from array import array
from dataclasses import dataclass, field

from evo_agents.kg import snapshot_text
from evo_agents.kg.graph_model import Clusters
from evo_agents.kg.pacing import let_go, paced


@dataclass(frozen=True)
class StoreRows:
    """What a snapshot is made of, the rows of one build: its content hash, its live node and edge rows
    (NODE_COLUMNS, EDGE_COLUMNS), its full-text rows keyed as ``node_fts_rows`` keeps them (folded), the folded text
    of its code by file node id, and its top label and audience."""

    content_hash: str
    nodes: tuple[tuple, ...]
    edges: tuple[tuple, ...]
    keyed: tuple[tuple[str, tuple], ...]
    code: dict[str, str]
    top: tuple[tuple[int, int], frozenset[str]]
    units: tuple[tuple, ...] = ()  # live units: UNIT_KEYS, then label_aud, label_lvl, label_loc
    wheres: tuple[tuple, ...] = ()  # live where-provenance: (element, unit, span), by element, unit, span
    derivations: tuple[tuple, ...] = ()  # live derivations: (element, units JSON, needs JSON), by element, deriv id
    summary: dict = field(default_factory=dict)  # ``SnapshotView.summary``
    vectors: tuple[tuple, ...] = ()  # the rows of node_vec: (key, node id, kind, aud, vector bytes), by key
    vector_info: dict | None = None  # ``semantic.Info`` of them as a dict; None without vectors
    # the rows of the asked index (node id, level, location, audience, prior, words), by ``asked_order``, and the
    # weight of an asked match the build recorded; empty and None without them
    asked: tuple[tuple, ...] = ()
    asked_weight: float | None = None
    # the build's clusters as ``cluster_rows`` gives them, and its review items
    # by fingerprint; empty without them
    clusters: tuple[tuple, ...] = ()
    review: tuple[dict, ...] = ()

    def __del__(self) -> None:
        let_go(self)  # as a snapshot's parts (``Snapshot.__del__``)


UNIT_KEYS = ("unit", "item", "anchor", "rev", "hash", "source", "span")


def asked_order(rows) -> tuple[tuple, ...]:
    """Asked rows in one order whatever read them: by node, then by the JSON of the rest (an audience may be None)."""
    return tuple(sorted((tuple(r) for r in rows), key=lambda r: (r[0], json.dumps(r[1:]))))


CLUSTER, MEMBER, LINK = "k", "m", "l"  # the kinds of a ``cluster_rows`` row


def cluster_rows(found: Clusters | None) -> tuple[tuple, ...]:
    """``found`` as rows: (CLUSTER, id, its dict), (MEMBER, node id, [top, sub]), (LINK, [level, a, b], weight)."""
    if found is None:
        return ()
    rows: list[tuple] = [(CLUSTER, cid, c) for cid, c in found.clusters.items()]
    rows += [(MEMBER, nid, list(m)) for nid, m in found.members.items()]
    rows += [(LINK, list(key), w) for key, w in found.links.items()]
    return cluster_order(rows)


def cluster_order(rows) -> tuple[tuple, ...]:
    """``cluster_rows`` rows in one order whatever read them: by kind, then by the JSON of the key."""
    return tuple(sorted(rows, key=lambda r: (r[0], json.dumps(r[1]))))


def rows_clusters(rows) -> Clusters | None:
    """The clusters of ``cluster_rows`` rows; None without a cluster, as ``SnapshotView.clusters``."""
    out = Clusters()
    for kind, key, value in paced(rows):
        if kind == CLUSTER:
            out.clusters[key] = dict(value)
        elif kind == MEMBER:
            out.members[key] = (value[0], value[1])
        else:
            out.links[(key[0], key[1], key[2])] = value
    return out if out.clusters else None


def rows_vectors(rows: StoreRows) -> snapshot_text.Vectors | None:
    """The vectors of ``rows`` (``StoreRows.vectors``), None without them."""
    from evo_agents.kg import semantic

    if not rows.vectors or not rows.vector_info:
        return None
    found = semantic.Info(**rows.vector_info)
    unpack = struct.Struct(f"<{found.dim}e").unpack
    return snapshot_text.Vectors(
        found,
        tuple(r[1] for r in paced(rows.vectors)),
        tuple(r[2] for r in paced(rows.vectors)),
        tuple(r[3] for r in paced(rows.vectors)),
        tuple(array("f", unpack(bytes(r[4]))) for r in paced(rows.vectors)),
    )
