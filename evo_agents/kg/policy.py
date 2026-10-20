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

from evo_agents.kg.protocol import PRINCIPAL

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


def any_of(labels) -> Label | None:
    """The label of an element that any one of its derivations, labelled ``labels``, reveals: ``meet_all`` in level,
    location, integrity and projects. In audience the meet (the intersection) would show the element to a reader in
    neither of two chats, so it takes the least audience of the derivations at the lowest level and location, else the
    join of theirs: a reader then sees the element only when it sees one derivation whole."""
    labels = list(labels)
    low = meet_all(labels)
    if low is None:
        return None
    lowest = [label.audience for label in labels if (label.level, label.location) == (low.level, low.location)]
    lowest = lowest or [label.audience for label in labels]
    least = next((a for a in lowest if all(audience_below(a, b) for b in lowest)), None)
    if least is None:
        least = frozenset().union(*lowest)
    return Label(low.level, low.location, low.integrity, low.projects, least)


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
