"""Sensitivity labels and sink clearances (definition D8).

A label is an element of L = levels x 2^projects x locations x integrity x 2^principals. The order is "may flow to":
a lower level, a subset of projects, a less restricted location, trusted integrity and a subset of principals sit
lower. A sink may read x when label(x) is below the sink's clearance. A source with no label is treated as the highest
level with integrity U (fail closed).

The principals are the label's audience, strings with a prefix: ``chat:<id>``, a member of that chat, and
``grant:<project>:<level>``, one with a grant on that project up to that level at least. A reader holds an audience
too, and sees an item only when the item's audience is a subset of the reader's, as with projects; an item with no
audience passes this dimension for every reader. Join takes the union, meet the intersection. An audience that cannot
be read is the highest, TOP_AUDIENCE, which only the clearance of a sink holds and no reader: the item is seen by none
(fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evo_agents.schema.canonical import PRINCIPAL

DEFAULT_LEVELS = ["public", "internal", "customer", "secret"]
DEFAULT_LOCATIONS = ["any"]
EVERY = "*"  # the principal that stands for all of them
TOP_AUDIENCE = frozenset({EVERY})  # the highest audience: above every other, and below only itself
_PRINCIPAL = re.compile(PRINCIPAL)


def audience_of(value) -> frozenset[str]:
    """The audience a kg/1 label's ``audience`` names: none for a missing key or an empty list; the principals of a
    sorted list of them without repeats; TOP_AUDIENCE for anything else."""
    if value is None or value == []:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(p, str) and _PRINCIPAL.match(p) for p in value):
        return TOP_AUDIENCE
    return frozenset(value) if value == sorted(set(value)) else TOP_AUDIENCE


def audience_join(a: frozenset[str], b: frozenset[str]) -> frozenset[str]:
    return TOP_AUDIENCE if EVERY in a or EVERY in b else a | b


def audience_meet(a: frozenset[str], b: frozenset[str]) -> frozenset[str]:
    return b if EVERY in a else a if EVERY in b else a & b


def audience_below(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether a reader holding audience ``b`` sees what audience ``a`` names."""
    return EVERY in b or (EVERY not in a and a <= b)


@dataclass(frozen=True)
class Label:
    level: int
    location: int
    integrity: str  # "T" (parser-derived, trusted) or "U" (free text, untrusted)
    projects: frozenset[str]
    audience: frozenset[str] = frozenset()  # principals a reader must all be (module docstring)

    def __post_init__(self):
        if EVERY in self.audience and self.audience != TOP_AUDIENCE:
            object.__setattr__(self, "audience", TOP_AUDIENCE)

    @classmethod
    def of(cls, label: tuple, projects=frozenset()) -> Label:
        """The label of a store row's (level, location, integrity) and, when it has one, audience."""
        return cls(label[0], label[1], label[2], frozenset(projects), label[3] if len(label) > 3 else frozenset())

    def join(self, other: Label) -> Label:
        return Label(
            max(self.level, other.level),
            max(self.location, other.location),
            "U" if "U" in (self.integrity, other.integrity) else "T",
            self.projects | other.projects,
            audience_join(self.audience, other.audience),
        )

    def meet(self, other: Label) -> Label:
        return Label(
            min(self.level, other.level),
            min(self.location, other.location),
            "T" if "T" in (self.integrity, other.integrity) else "U",
            self.projects & other.projects,
            audience_meet(self.audience, other.audience),
        )

    def below(self, other: Label) -> bool:
        return (
            self.level <= other.level
            and self.location <= other.location
            and (self.integrity == "T" or other.integrity == "U")
            and self.projects <= other.projects
            and audience_below(self.audience, other.audience)
        )


def join_all(labels) -> Label | None:
    out = None
    for label in labels:
        out = label if out is None else out.join(label)
    return out


def meet_all(labels) -> Label | None:
    out = None
    for label in labels:
        out = label if out is None else out.meet(label)
    return out


def covers(p: Label, q: Label) -> bool:
    """Whether every reader ``q`` lets read also reads under ``p``: ``p`` is at most ``q`` in level, location, projects
    and audience, the dimensions a reader is checked on (``PolicyDNF.allows``). Integrity is no condition on a reader."""
    return (
        p.level <= q.level
        and p.location <= q.location
        and p.projects <= q.projects
        and audience_below(p.audience, q.audience)
    )


def _readable_key(label: Label) -> tuple:
    """The dimensions ``covers`` compares, in the order ``PolicyDNF`` sorts its atoms by: lowest level and location
    first."""
    return (label.level, label.location, tuple(sorted(label.projects)), tuple(sorted(label.audience)))


def _antichain(labels, integrity: str | None = None) -> tuple[Label, ...]:
    """The minimal normal form of the disjunction of ``labels``: the atoms no other atom covers, one for each readable
    dimensions, sorted by ``_readable_key``, all of integrity ``integrity`` (by default ``T`` when one of ``labels``
    is). An atom a different one covers is dropped, as every reader it lets read the other already lets. A function of
    the set of ``labels`` alone, so the disjunction it normalizes is commutative, associative and idempotent."""
    labels = list(labels)
    if integrity is None:
        integrity = "T" if any(label.integrity == "T" for label in labels) else "U"
    by_key = {_readable_key(label): label for label in labels}
    atoms = list(by_key.values())
    if len(atoms) > 1:
        atoms = [a for a in atoms if not any(b is not a and covers(b, a) for b in atoms)]
    return tuple(
        a if a.integrity == integrity else Label(a.level, a.location, integrity, a.projects, a.audience)
        for a in sorted(atoms, key=_readable_key)
    )


@dataclass(frozen=True)
class PolicyDNF:
    """Who may read an element some derivations reveal: a disjunction of atoms, each a ``Label`` a reader must all be
    within, kept as an antichain under ``covers`` (no atom lets read a reader another one does not), sorted lowest
    level and location first.

    ``disjoin`` is the union of independent derivations (the element shows when one of them does), ``conjoin`` the
    join of what one derivation needs (it shows when all of them do: ``join_all`` within one derivation, distributed
    over the atoms). Integrity is no condition on a reader and is kept apart from the atoms' order: every atom of a
    policy has the policy's, ``T`` when one derivation is trusted (the meet) and ``U`` when one thing a derivation
    uses is not (the join). Both are commutative, associative and idempotent, and ``allows`` is a homomorphism of the
    semiring into the booleans: ``allows(P.disjoin(Q), r) == allows(P, r) or allows(Q, r)`` and
    ``allows(P.conjoin(Q), r) == allows(P, r) and allows(Q, r)``. The empty disjunction (``never``) lets nobody read;
    ``always`` (the least label) lets every reader. Unlike the meet of labels, the disjunction never takes the level of
    one derivation and the location of another, so an element of derivations at (1, 0) and (0, 1) is hidden from a
    reader at (0, 0); nor does it join the audiences of two chats, so a member of either chat alone sees it.

    The first atom is the element's ``summary``: the label the single-label columns of its row hold (``label_lvl``,
    ``label_loc``, ``label_int``, ``label_aud``), so a reader that knows no other atom reads it only when that atom
    lets it."""

    atoms: tuple[Label, ...] = ()

    @classmethod
    def of(cls, *labels: Label) -> PolicyDNF:
        return cls(_antichain(labels))

    @classmethod
    def never(cls) -> PolicyDNF:
        return cls(())

    @classmethod
    def always(cls) -> PolicyDNF:
        return cls((Label(0, 0, "T", frozenset()),))

    def __iter__(self):
        return iter(self.atoms)

    def __len__(self) -> int:
        return len(self.atoms)

    @property
    def integrity(self) -> str:
        """``T`` when what the element says is trusted: one atom's, as every atom has the same (``_antichain``)."""
        return self.atoms[0].integrity if self.atoms else "T"

    def disjoin(self, other: PolicyDNF) -> PolicyDNF:
        if not other.atoms or other.atoms == self.atoms:
            return self
        if not self.atoms:
            return other
        return PolicyDNF(_antichain((*self.atoms, *other.atoms)))

    def conjoin(self, other: PolicyDNF) -> PolicyDNF:
        if len(self.atoms) == 1 and len(other.atoms) == 1:
            return PolicyDNF((self.atoms[0].join(other.atoms[0]),))
        if not self.atoms or not other.atoms:
            return PolicyDNF.never()
        integrity = "U" if "U" in (self.integrity, other.integrity) else "T"
        return PolicyDNF(_antichain((p.join(q) for p in self.atoms for q in other.atoms), integrity))

    def allows(self, level: int, location: int, holds: frozenset[str] = frozenset()) -> bool:
        """Whether a reader up to ``level`` and ``location`` that holds ``holds`` (the projects and principals it is)
        reads: one atom at most that level and location whose projects and audience are within ``holds``."""
        return self.first(level, location, holds) is not None

    def first(self, level: int, location: int, holds: frozenset[str] = frozenset()) -> int | None:
        """The index of the first atom that lets the reader of ``allows`` read, None when none does."""
        for i, atom in enumerate(self.atoms):
            if (
                atom.level <= level
                and atom.location <= location
                and atom.projects <= holds
                and audience_below(atom.audience, holds)
            ):
                return i
        return None

    def admits(self, reader: Label) -> bool:
        """Whether one atom lets read every reader ``reader`` stands for (``covers``)."""
        return any(covers(atom, reader) for atom in self.atoms)

    @property
    def summary(self) -> Label | None:
        return self.atoms[0] if self.atoms else None


@dataclass(frozen=True)
class Clearance:
    sink: str
    kind: str
    level: int
    location: int


class Policy:
    def __init__(self, project: str, knowledge: dict):
        policy = knowledge.get("policy") or {}
        self.project = project
        self.levels: list[str] = list(policy.get("levels") or DEFAULT_LEVELS)
        self.locations: list[str] = list(policy.get("locations") or DEFAULT_LOCATIONS)
        self.memo = policy.get("memo", "per-project")
        self.sinks: dict[str, Clearance] = {}
        for sink in policy.get("sinks") or []:
            clr = sink.get("clearance") or {}
            self.sinks[sink["id"]] = Clearance(
                sink["id"],
                sink.get("kind", "agent-session"),
                self._level(clr.get("level")),
                self._location(clr.get("location"), default_low=True),
            )
        self._sources = {s["id"]: s for s in knowledge.get("sources") or []}

    def _level(self, name: str | None) -> int:
        if name in self.levels:
            return self.levels.index(name)
        return len(self.levels) - 1

    def _location(self, name: str | None, *, default_low: bool = False) -> int:
        if name in self.locations:
            return self.locations.index(name)
        if name is None and default_low:
            return len(self.locations) - 1  # a sink with no location restriction may receive any location
        return 0 if name is None else len(self.locations) - 1

    def level_name(self, level: int) -> str:
        return self.levels[level]

    def location_name(self, location: int) -> str:
        return self.locations[location]

    def declares(self, source_id: str) -> bool:
        """Whether knowledge.yaml lists source ``source_id``; a source the hub keeps (a core plugin's, an upload's) is
        not listed there."""
        return source_id in self._sources

    def source_label(self, source_id: str, raised: dict | None = None) -> Label:
        """Label of everything a source says, raised (never lowered) by the item's own label."""
        source = self._sources.get(source_id)
        declared = (source or {}).get("label")
        if not declared:
            base = Label(len(self.levels) - 1, len(self.locations) - 1, "U", frozenset({self.project}))
        else:
            base = Label(
                self._level(declared.get("level")),
                self._location(declared.get("location")),
                declared.get("integrity", "U"),
                frozenset({self.project}),
            )
        if raised:
            extra = Label(
                self._level(raised["level"]) if raised.get("level") in self.levels else 0,
                self._location(raised["location"]) if raised.get("location") in self.locations else 0,
                raised.get("integrity", "T"),
                frozenset({self.project}),
                audience_of(raised.get("audience")),
            )
            base = base.join(extra)
        return base

    def clearance(self, sink: str) -> Clearance:
        if sink not in self.sinks:
            raise KeyError(f"sink {sink!r} is not declared in knowledge.yaml policy.sinks")
        return self.sinks[sink]

    def allows(self, label: Label, sink: str) -> bool:
        clr = self.clearance(sink)
        return label.level <= clr.level and label.location <= clr.location and label.projects <= {self.project}

    def hub_sink(self) -> Clearance | None:
        """The project's sink of kind hub, None when it declares none."""
        return next((sink for sink in self.sinks.values() if sink.kind == "hub"), None)

    def hub_clears(self, source_id: str) -> bool | None:
        """Whether the hub sink clears the label of source ``source_id``, so its runs may leave the machine for the hub
        (``hub kg push``); None when the project declares no hub sink."""
        hub = self.hub_sink()
        if hub is None:
            return None
        label = self.source_label(source_id)
        return label.level <= hub.level and label.location <= hub.location

    def describe(self, label: Label) -> dict:
        """The label by names; ``audience``, sorted, only when it names one."""
        named = {
            "level": self.level_name(label.level),
            "location": self.location_name(label.location),
            "integrity": label.integrity,
        }
        return {**named, "audience": sorted(label.audience)} if label.audience else named
