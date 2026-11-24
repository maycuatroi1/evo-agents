"""What a build changed of a snapshot's rows (``Delta``), and the snapshot it makes of one (``apply``,
``Snapshot.apply_changes``): the snapshot ``Snapshot.from_stored`` would make of every row then, made from the old
one's parts. The nodes and edges not touched keep their dicts, the text index cuts only the new rows into tokens
(``TextIndex.updated``), and the numbering of the nodes, the CSR partitions, names, counts and policies are made
again, as one node added renumbers those after it.

A delta names every key it touched, of each kind of row, and gives the rows those keys hold now: a key touched
without a row is gone. ``evo_agents.hub.kg_graph_delta`` reads one from the hub's tables."""

from __future__ import annotations

import bisect
import operator
from array import array
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from evo_agents.kg import snapshot_carry, snapshot_masks
from evo_agents.kg.pacing import paced
from evo_agents.kg.snapshot import Shared, Snapshot, count_units, edge_dict, edge_order, node_dict
from evo_agents.kg.snapshot_rows import rows_clusters

try:
    import numpy as _np
except ImportError:  # the ends of the edges are then read one edge at a time
    _np = None

_ID = operator.itemgetter("id")


class DeltaError(ValueError):
    """A delta that does not apply to the snapshot it was given: read the rows whole instead."""


@dataclass(frozen=True)
class Delta:
    """What a build changed: per kind of row, the keys it touched and the rows those keys hold now."""

    base: str  # the content hash of the snapshot it applies to
    content_hash: str  # the content hash of the build that made it
    top: tuple[tuple[int, int], frozenset[str]]
    summary: dict
    build: int = 0
    node_ids: tuple[str, ...] = ()  # the nodes touched; ``nodes`` holds the NODE_COLUMNS rows of those still live
    nodes: tuple[tuple, ...] = ()
    edge_ids: tuple[str, ...] = ()  # the edges touched; ``edges`` holds their live rows, and those of the edges of
    edges: tuple[tuple, ...] = ()  # a node touched, which an old snapshot left out while one end was missing
    text_keys: tuple[str, ...] = ()  # the full-text rows touched, by key; ``keyed`` holds their rows now
    keyed: tuple[tuple[str, tuple], ...] = ()
    code_ids: tuple[str, ...] = ()  # the code files touched; ``code`` holds the folded text of those still live
    code: dict[str, str] = field(default_factory=dict)
    # per element (or unit) touched: its unit rows, where-provenance (element, unit, span) and derivations (element,
    # units, needs) now
    provenance: dict[str, tuple[tuple, tuple, tuple]] = field(default_factory=dict)
    asked: tuple[tuple, ...] | None = None  # the asked rows whole when they changed, else None
    asked_weight: float | None = None
    clusters: tuple[tuple, ...] | None = None  # the cluster rows whole when they changed, else None
    review: tuple[dict, ...] | None = None  # the review items whole when they changed, else None


def apply(snap: Snapshot, delta: Delta) -> Snapshot:
    """The snapshot ``delta`` makes of ``snap``; raises DeltaError when ``snap`` is not the one it applies to."""
    if snap.content_hash != delta.base:
        raise DeltaError(f"the delta applies to {delta.base}, not to {snap.content_hash}")
    read, held, node_old, edge_old = _graph(snap, delta)
    try:
        text = snap.text.updated(delta.keyed, frozenset(delta.text_keys), delta.code, frozenset(delta.code_ids))
    except ValueError as e:
        raise DeltaError(str(e)) from e
    parts = {"text": text, "vectors": snap.vectors, "build": delta.build, "top": delta.top}
    made = snapshot_carry.made(snap, read, held, node_old, edge_old, content_hash=delta.content_hash, **parts)
    if made is None:
        made = Snapshot.made(delta.content_hash, read, held, **parts)
    gone = frozenset(delta.text_keys)
    bodies = {k: v for k, v in paced(snap.bodies.items()) if k not in gone}
    bodies.update((key, (row[4], row[5] if len(row) > 5 else None)) for key, row in delta.keyed)
    units, where, derivations = _provenance(snap, delta.provenance)
    counts = (
        count_units(units.values())
        if units is not snap.units
        else (snap.unit_counts, snap.item_counts, snap.mixed_items, snap.audience_sources)
    )
    out = replace(
        made,
        units=units,
        where=where,
        derivations=derivations,
        bodies=MappingProxyType(bodies),
        summary=MappingProxyType(dict(delta.summary)),
        asked=tuple(delta.asked) if delta.asked is not None else snap.asked,
        asked_weight=delta.asked_weight,
        found=rows_clusters(delta.clusters) if delta.clusters is not None else snap.found,
        review=tuple(delta.review) if delta.review is not None else snap.review,
        unit_counts=counts[0],
        item_counts=counts[1],
        mixed_items=counts[2],
        audience_sources=counts[3],
    )
    return snapshot_masks.carried(made, out)


def _graph(snap: Snapshot, delta: Delta) -> tuple[list[dict], list[dict], array, array]:
    """(the nodes by id, the edges by ``edge_order``, each with both ends among the nodes, and per node and per edge
    its number in ``snap``, -1 for one ``delta`` wrote) after ``delta``."""
    touched = frozenset(delta.node_ids)
    fresh = sorted(map(node_dict, delta.nodes), key=lambda n: n["id"])
    if any(n["id"] not in touched for n in fresh):
        raise DeltaError("a node row the delta does not name")
    one = Shared(tuple(n["id"] for n in fresh))
    for n in fresh:
        n["kind"], n["status"], n["label"] = one.text(n["kind"]), one.text(n["status"]), one.label(n["label"])
    kept = array("i", (i for i, n in enumerate(paced(snap.nodes)) if n["id"] not in touched))
    read, node_old = _merged([snap.nodes[i] for i in kept], kept, fresh, lambda n: n["id"])
    alive = frozenset(n["id"] for n in paced(read))
    rows = {row[0]: row for row in delta.edges}  # an edge of a node touched may be named too
    changed = frozenset(delta.edge_ids) | rows.keys()
    kept = _kept_edges(snap, changed, touched - alive)
    new = [one.edge(e) for e in map(edge_dict, rows.values()) if e["src"] in alive and e["dst"] in alive]
    held, edge_old = _merged([snap.edges[i] for i in kept], kept, sorted(new, key=edge_order), edge_order)
    return read, held, node_old, edge_old


def _kept_edges(snap: Snapshot, changed: frozenset[str], gone: frozenset[str]) -> array:
    """The numbers of the edges of ``snap`` neither ``changed`` nor of a node ``gone``, ascending: the ends of an edge
    read by its node numbers (``Snapshot.src``, ``dst``), not by its dict."""
    drop = {i for i, edge_id in enumerate(paced(map(_ID, snap.edges))) if edge_id in changed}
    ends = {snap.index[nid] for nid in gone if nid in snap.index}
    if ends and _np is not None and len(snap.src):
        nodes = _np.fromiter(ends, dtype=_np.uint32, count=len(ends))
        src, dst = (_np.frombuffer(a, dtype=_np.uint32) for a in (snap.src, snap.dst))
        drop.update(_np.flatnonzero(_np.isin(src, nodes) | _np.isin(dst, nodes)).tolist())
    elif ends:
        pairs = enumerate(paced(zip(snap.src, snap.dst, strict=True)))
        drop.update(i for i, pair in pairs if not ends.isdisjoint(pair))
    kept, last = array("i"), 0
    for i in sorted(drop):
        kept.extend(range(last, i))
        last = i + 1
    kept.extend(range(last, len(snap.edges)))
    return kept


def _merged(kept: list, numbers: array, fresh: list, key) -> tuple[list, array]:
    """(``kept`` and ``fresh``, both by ``key``, merged by it; per item, its number in ``numbers`` or -1 if fresh)."""
    out: list = []
    old = array("i")
    last = 0
    for x in fresh:
        at = bisect.bisect_left(kept, key(x), lo=last, key=key)
        out += kept[last:at]
        old += numbers[last:at]
        out.append(x)
        old.append(-1)
        last = at
    out += kept[last:]
    old += numbers[last:]
    return out, old


def _provenance(snap: Snapshot, changed: dict[str, tuple[tuple, tuple, tuple]]):
    """(units, where, derivations) of ``snap`` with the rows of each element of ``changed`` in place of its own;
    those of ``snap`` themselves when nothing changed."""
    if not changed:
        return snap.units, snap.where, snap.derivations
    units, where, derivations = dict(snap.units), dict(snap.where), dict(snap.derivations)
    for elem in changed:
        units.pop(elem, None)
        where.pop(elem, None)
        derivations.pop(elem, None)
    wheres: dict[str, list] = {}
    derived: dict[str, list] = {}
    for unit_rows, where_rows, derived_rows in changed.values():
        units.update((r[0], tuple(r)) for r in unit_rows)
        for e, unit, span in sorted(where_rows, key=lambda w: (w[0], w[1], w[2] is not None, w[2] or "")):
            wheres.setdefault(e, []).append((unit, span))  # in the order ``kg_graph_rows.assembled_rows`` reads them
        for e, unit_list, needs in derived_rows:
            derived.setdefault(e, []).append((unit_list, needs))
    where.update((k, tuple(v)) for k, v in wheres.items())
    derivations.update((k, tuple(v)) for k, v in derived.items())
    return MappingProxyType(dict(sorted(units.items()))), MappingProxyType(where), MappingProxyType(derivations)
