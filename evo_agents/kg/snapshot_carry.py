"""The parts of a snapshot after a delta (``evo_agents.kg.snapshot_delta``) carried from the old snapshot's with
numpy, instead of read again from every node and edge as ``Snapshot.made`` reads them: the node numbers of the
edges' ends, the partitions' edges, the policies, the counts and the names. What a part holds is what
``Snapshot.made`` gives of the same nodes and edges; the counts may list their keys in another order. Without
numpy ``made`` answers None and the delta makes the snapshot with ``Snapshot.made``."""

from __future__ import annotations

import itertools
from array import array
from collections import Counter
from types import MappingProxyType

from evo_agents.kg import snapshot_masks
from evo_agents.kg.snapshot import Snapshot, partitions_of

try:
    import numpy as _np
except ImportError:  # the delta then makes the snapshot with ``Snapshot.made``
    _np = None


class Carry:
    """Per node and per edge after the delta, its number in the old snapshot, -1 for one the delta wrote; and per
    node of the old snapshot, its number after the delta, -1 for one the delta wrote or dropped."""

    def __init__(self, snap: Snapshot, node_old: array, edge_old: array):
        np = _np
        assert np is not None
        self.snap = snap
        self.node_old = np.frombuffer(node_old, dtype=np.int32).astype(np.int64) if len(node_old) else _ints(0)
        self.edge_old = np.frombuffer(edge_old, dtype=np.int32).astype(np.int64) if len(edge_old) else _ints(0)
        self.remap = np.full(len(snap.nodes), -1, dtype=np.int64)
        kept = self.node_old >= 0
        self.remap[self.node_old[kept]] = np.flatnonzero(kept)


def _ints(n: int):
    assert _np is not None
    return _np.zeros(n, dtype=_np.int64)


def _uint32(values) -> array:
    assert _np is not None
    out = array("I")
    out.frombytes(values.astype(_np.uint32).tobytes())
    return out


def made(
    snap: Snapshot, read: list[dict], held: list[dict], node_old: array, edge_old: array, **parts
) -> Snapshot | None:
    """The snapshot of node dicts ``read`` and edge dicts ``held`` (``Snapshot.made``), its parts carried from
    ``snap`` by the old numbers ``node_old`` and ``edge_old``; ``parts`` are ``Snapshot.made``'s other arguments.
    None without numpy, or when the names cannot be carried (a node with aliases touched)."""
    if _np is None:
        return None
    carry = Carry(snap, node_old, edge_old)
    names = _names(carry, read)
    if names is None:
        return None
    ids = tuple(n["id"] for n in read)
    index = {nid: i for i, nid in enumerate(ids)}
    src = _ends(carry, held, snap.src, index, "src")
    dst = _ends(carry, held, snap.dst, index, "dst")
    policies = _policies(carry, read, held)
    node_counts, edge_counts, shape = _counts(carry, read, held, src, dst, policies)
    top = parts["top"]
    out = Snapshot(
        content_hash=parts["content_hash"],
        ids=ids,
        index=MappingProxyType(index),
        nodes=tuple(read),
        edges=tuple(held),
        src=_uint32(src),
        dst=_uint32(dst),
        partitions=partitions_of(len(ids), _uint32(src), _uint32(dst), _groups(carry, held)),
        names=MappingProxyType(names),
        aliases=snap.aliases,
        text=parts["text"],
        vectors=parts["vectors"],
        build=parts["build"],
        top_label=top[0],
        top_audience=top[1],
        node_counts=node_counts,
        edge_counts=edge_counts,
        shape=shape,
    )
    object.__setattr__(out, "_policies", policies)  # as ``snapshot_masks.of`` keeps them
    return out


def _ends(carry: Carry, held: list[dict], old: array, index: dict[str, int], end: str):
    """Per edge of ``held``, the number of its ``end`` node: carried for an edge kept whose end was not written."""
    np = _np
    assert np is not None
    out = np.full(len(held), -1, dtype=np.int64)
    kept = carry.edge_old >= 0
    if len(old):
        before = np.frombuffer(old, dtype=np.uint32).astype(np.int64)
        out[kept] = carry.remap[before[carry.edge_old[kept]]]
    for i in np.flatnonzero(out < 0).tolist():
        out[i] = index[held[i][end]]
    return out


def _groups(carry: Carry, held: list[dict]) -> dict:
    """The edge numbers per (relation, status), as ``Snapshot.made`` groups them, as numpy arrays."""
    np = _np
    assert np is not None
    keys = list(carry.snap.partitions)
    codes = {k: c for c, k in enumerate(keys)}
    before = np.zeros(len(carry.snap.edges), dtype=np.int64)
    for c, part in enumerate(carry.snap.partitions.values()):
        if len(part.out_edges):
            before[np.frombuffer(part.out_edges, dtype=np.uint32)] = c
    group = np.full(len(held), -1, dtype=np.int64)
    kept = carry.edge_old >= 0
    group[kept] = before[carry.edge_old[kept]]
    for i in np.flatnonzero(~kept).tolist():
        e = held[i]
        group[i] = codes.setdefault((e["rel"], e["status"] or ""), len(codes))
    keys = list(codes)
    order = np.argsort(group, kind="stable")
    sizes = np.bincount(group, minlength=len(keys)) if len(group) else _ints(len(keys))
    starts = np.concatenate([[0], np.cumsum(sizes)])
    return {keys[c]: order[starts[c] : starts[c + 1]] for c in range(len(keys)) if sizes[c]}


def _policies(carry: Carry, read: list[dict], held: list[dict]) -> snapshot_masks.Policies:
    """The policies of the nodes and edges: the old snapshot's numbers for those kept, its keys first."""
    np = _np
    assert np is not None
    old = snapshot_masks.of(carry.snap)
    numbers = {k: i for i, k in enumerate(old.keys)}
    out = snapshot_masks.Policies.__new__(snapshot_masks.Policies)
    for name, rows, was in (("node", read, carry.node_old), ("edge", held, carry.edge_old)):
        before = np.frombuffer(getattr(old, name), dtype=np.uint32).astype(np.int64) if len(rows) else _ints(0)
        got = np.zeros(len(rows), dtype=np.int64)
        kept = was >= 0
        got[kept] = before[was[kept]]
        for i in np.flatnonzero(~kept).tolist():
            got[i] = numbers.setdefault(snapshot_masks.policy(rows[i]), len(numbers))
        setattr(out, name, _uint32(got))
    out.keys = tuple(numbers)
    return out


def _counts(carry: Carry, read, held, src, dst, policies) -> tuple[tuple, tuple, tuple]:
    """(node_counts, edge_counts, shape) as ``snapshot._graph_counts`` counts them: the edges' by policy number, the
    nodes' and the shapes' as the old snapshot's, less those of the nodes and edges written, plus theirs now."""
    np = _np
    assert np is not None
    snap, policy = carry.snap, snapshot_masks.policy
    edges = np.frombuffer(policies.edge, dtype=np.uint32) if len(held) else _ints(0)
    per = np.bincount(edges, minlength=len(policies.keys)) if len(edges) else _ints(len(policies.keys))
    edge_counts = tuple((policies.keys[i], int(c)) for i, c in enumerate(per.tolist()) if c)
    gone = np.flatnonzero(carry.remap < 0)  # the old nodes written or dropped
    fresh = np.flatnonzero(carry.node_old < 0)
    nodes = Counter({(kind, key): c for kind, key, c in snap.node_counts})
    nodes.subtract((snap.nodes[i]["kind"], policy(snap.nodes[i])) for i in gone.tolist())
    nodes.update((read[i]["kind"], policy(read[i])) for i in fresh.tolist())
    shape = Counter({k[:-1]: k[-1] for k in snap.shape})
    old_src = np.frombuffer(snap.src, dtype=np.uint32) if len(snap.src) else _ints(0)
    old_dst = np.frombuffer(snap.dst, dtype=np.uint32) if len(snap.dst) else _ints(0)
    left = np.ones(len(snap.edges), dtype=bool)
    left[carry.edge_old[carry.edge_old >= 0]] = False
    left |= np.isin(old_src, gone) | np.isin(old_dst, gone)
    shape.subtract(_shape(snap.nodes, snap.edges, old_src, old_dst, np.flatnonzero(left)))
    came = (carry.edge_old < 0) | np.isin(src, fresh) | np.isin(dst, fresh)
    shape.update(_shape(read, held, src, dst, np.flatnonzero(came)))
    return (
        tuple((*k, c) for k, c in nodes.items() if c > 0),
        edge_counts,
        tuple((*k, c) for k, c in shape.items() if c > 0),
    )


def _shape(nodes, edges, src, dst, which):
    for i in which.tolist():
        e, s, d = edges[i], nodes[int(src[i])], nodes[int(dst[i])]
        yield (s["kind"], e["rel"], d["kind"], e["label"], s["label"], d["label"])


def _names(carry: Carry, read: list[dict]) -> dict | None:
    """The node numbers per id, name and alias (``Snapshot.made``), the old snapshot's renumbered; None when a node
    written, before or after, has aliases, as which node an alias names is then read again."""
    np = _np
    assert np is not None
    snap = carry.snap
    gone = np.flatnonzero(carry.remap < 0).tolist()
    fresh = np.flatnonzero(carry.node_old < 0).tolist()
    if any(snap.nodes[i]["aliases"] for i in gone) or any(read[i]["aliases"] for i in fresh):
        return None
    keys = list(snap.names)
    sizes = np.fromiter(map(len, snap.names.values()), dtype=np.int64, count=len(keys))
    flat = np.fromiter(itertools.chain.from_iterable(snap.names.values()), dtype=np.int64, count=int(sizes.sum()))
    renumbered = carry.remap[flat] if len(flat) else flat
    held = iter(renumbered.tolist())
    out = {k: tuple(itertools.islice(held, n)) for k, n in zip(keys, sizes.tolist(), strict=True)}
    if len(flat):
        for c in set(np.repeat(np.arange(len(keys)), sizes)[renumbered < 0].tolist()):
            kept = tuple(i for i in out[keys[c]] if i >= 0)
            if kept:
                out[keys[c]] = kept
            else:
                del out[keys[c]]
    for i in fresh:
        n = read[i]
        for name in {x for x in (n["id"], n["name"]) if isinstance(x, str)}:
            out[name] = tuple(sorted((*out.get(name, ()), i)))
    return out
