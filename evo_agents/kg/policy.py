"""Sensitivity labels and sink clearances (definition D8).

A label is an element of L = levels x 2^projects x locations x integrity. The order is "may flow to":
a lower level, a subset of projects, a less restricted location and trusted integrity sit lower.
A sink may read x when label(x) is below the sink's clearance. A source with no label is treated as
the highest level with integrity U (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LEVELS = ["public", "internal", "customer", "secret"]
DEFAULT_LOCATIONS = ["any"]


@dataclass(frozen=True)
class Label:
    level: int
    location: int
    integrity: str  # "T" (parser-derived, trusted) or "U" (free text, untrusted)
    projects: frozenset[str]

    def join(self, other: Label) -> Label:
        return Label(
            max(self.level, other.level),
            max(self.location, other.location),
            "U" if "U" in (self.integrity, other.integrity) else "T",
            self.projects | other.projects,
        )

    def meet(self, other: Label) -> Label:
        return Label(
            min(self.level, other.level),
            min(self.location, other.location),
            "T" if "T" in (self.integrity, other.integrity) else "U",
            self.projects & other.projects,
        )

    def below(self, other: Label) -> bool:
        return (
            self.level <= other.level
            and self.location <= other.location
            and (self.integrity == "T" or other.integrity == "U")
            and self.projects <= other.projects
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

    def describe(self, label: Label) -> dict:
        return {
            "level": self.level_name(label.level),
            "location": self.location_name(label.location),
            "integrity": label.integrity,
        }
