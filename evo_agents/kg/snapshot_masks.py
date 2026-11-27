"""Who reads each element of a snapshot (``evo_agents.kg.snapshot``), compiled once per build and evaluated once
per reader, not once per node or edge read.

An element's policy is its label, or for an element with guards (a ``PolicyDNF`` of the labels of the items it
derives from, ``graph_model.read_as``) the labels of its guards, any of which lets a reader in. A build holds few
distinct policies against many elements: ``Policies`` numbers them, each node and edge by its policy's number.
``Sight`` is one reader's view of them: ``sees`` holds, per policy, whether the reader's audience is within one of
its labels', and ``within(ceiling)`` whether, besides, one such label is at most ``ceiling`` (level, location). A
view's reads then look a byte up per element."""

from __future__ import annotations

import threading
from array import array

from evo_agents.kg.pacing import paced
from evo_agents.schema.policy import audience_below

_MADE = threading.Lock()  # one thread compiles a snapshot's policies
WITHIN_HELD = 64  # ceilings a sight keeps the mask of


def policy(x: dict) -> tuple:
    """What decides who reads element ``x``: its label, or the labels of its guards."""
    guards = x.get("guards")
    return tuple(g[0] for g in guards) if guards else x["label"]


def guarded(key: tuple) -> bool:
    """Whether a policy is that of an element with guards: a tuple of labels, not one label."""
    return bool(key) and isinstance(key[0], tuple)


def seen_by(key: tuple, reader: frozenset[str]) -> bool:
    """Whether a reader of audience ``reader`` reads an element of policy ``key``: of one of its labels."""
    return any(audience_below(a[3], reader) for a in key) if guarded(key) else audience_below(key[3], reader)


def under(key: tuple, reader: frozenset[str], ceiling: tuple[int, int] | None) -> bool:
    """Whether an element of policy ``key`` is at most ``ceiling``: for one with guards, by a label the reader
    reads (``SnapshotView.under``)."""
    if ceiling is None:
        return True
    if guarded(key):
        return any(audience_below(a[3], reader) and a[0] <= ceiling[0] and a[1] <= ceiling[1] for a in key)
    return key[0] <= ceiling[0] and key[1] <= ceiling[1]


def label_read(key: tuple, reader: frozenset[str], ceiling: tuple[int, int] | None) -> tuple | None:
    """The label a reader reads an element of policy ``key`` with, as ``graph_model.read_as`` picks it; None when
    the reader reads none of its labels."""
    if not guarded(key):
        return key if audience_below(key[3], reader) else None
    seen = [a for a in key if audience_below(a[3], reader)]
    if not seen:
        return None
    within = [a for a in seen if ceiling is None or (a[0] <= ceiling[0] and a[1] <= ceiling[1])]
    return (within or seen)[0]


class Policies:
    """The distinct policies of a snapshot's elements (``keys``), and each node's and edge's number among them."""

    def __init__(self, nodes, edges):
        numbers: dict[tuple, int] = {}
        by_label: dict[int, int] = {}  # a snapshot's labels are interned (``Shared``): most are found by identity

        def number(x: dict) -> int:
            if "guards" not in x:
                label = x["label"]
                found = by_label.get(id(label))
                if found is None:
                    found = by_label[id(label)] = numbers.setdefault(label, len(numbers))
                return found
            return numbers.setdefault(policy(x), len(numbers))

        self.node = array("I", (number(n) for n in paced(nodes)))
        self.edge = array("I", (number(e) for e in paced(edges)))
        self.keys = tuple(numbers)


def of(snap) -> Policies:
    """The policies of ``snap``, compiled at its first read and kept on it."""
    found = snap.__dict__.get("_policies")
    if found is None:
        with _MADE:
            found = snap.__dict__.get("_policies")
            if found is None:
                found = Policies(snap.nodes, snap.edges)
                object.__setattr__(snap, "_policies", found)  # a snapshot is frozen; this is a cache of it
    return found


def carried(old, new):
    """``new``, a copy of snapshot ``old`` with other provenance or reads but the same nodes and edges, with the
    policies ``old`` compiled."""
    found = old.__dict__.get("_policies")
    if found is not None and new.nodes is old.nodes and new.edges is old.edges:
        object.__setattr__(new, "_policies", found)
    return new


class Sight:
    """The policies of a snapshot as one reader reads them: per policy number, a byte."""

    def __init__(self, policies: Policies, reader: frozenset[str]):
        self.policies, self.reader = policies, reader
        self.sees = bytes(seen_by(k, reader) for k in policies.keys)
        self._within: dict[tuple[int, int] | None, bytes] = {}

    def within(self, ceiling: tuple[int, int] | None) -> bytes:
        """Per policy, whether the reader reads it and it is at most ``ceiling``."""
        found = self._within.get(ceiling)
        if found is None:
            reader, keys = self.reader, self.policies.keys
            found = bytes(bool(s) and under(k, reader, ceiling) for s, k in zip(self.sees, keys, strict=True))
            if len(self._within) < WITHIN_HELD:
                self._within[ceiling] = found
        return found
