"""Who may read and who may push what in a hub project, on the label lattice of ``evo_agents.kg.policy`` (D8).

A project on the hub keeps the ladder (levels, locations) and the sinks of its knowledge.yaml, and every object in
it carries a label by names, ``{level, location, integrity, projects}``. Two rules decide everything:

- Reading: an object labelled L, read through sink S of project P by a member whose grant reaches level M, is
  visible only when L is below meet(label of M, clearance of S) and L.projects is a subset of {P}.
- Pushing: the member needs the writer role, and L must be below the clearance of P's sink of kind ``hub``. A
  project that declares no such sink takes no push at all: data leaves a machine only for a project whose
  knowledge.yaml says which levels may go to the hub.

Reading on the hub itself, where no runtime sink takes part (the web shows members their own view), drops the sink
from the first rule: L must be below the label of M, with L.projects a subset of {P} (``visible_by_grant``).

All of them fail closed. When reading, a level or location the ladder lacks counts as the highest, a label that cannot
be read hides its object, and a max level or sink the project lacks lets nothing through. When pushing, a name the
ladder lacks is refused. The lattice itself (meet, below) is ``Label``'s; this module maps names onto it.

Standard library only, besides ``evo_agents.kg.policy``: the server enforces the rules, and the client can say
before a push what would be refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from evo_agents.kg.policy import Clearance, Label, Policy

ROLES = ("reader", "writer", "admin")  # each role may do what the ones before it may
HUB_KIND = "hub"
INTEGRITIES = ("T", "U")
UNTRUSTED = "U"  # a grant or a clearance says nothing about integrity, so it admits both


class Refused(Exception):
    """A push the rules refuse; ``status`` is the HTTP status the hub answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def has_role(role: str | None, needed: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


def _rank(names: Sequence[str], name) -> int | None:
    return names.index(name) if isinstance(name, str) and name in names else None


def hub_sink_example(level: str, location: str | None = None) -> str:
    """A sink of kind hub as knowledge.yaml declares it, clearing ``level`` (and ``location``)."""
    clearance = f"level: {level}" + (f", location: {location}" if location else "")
    return f"{{id: hub, kind: hub, clearance: {{{clearance}}}}}"


class ProjectRules:
    """The ladder and the sinks of one project as the hub registered them, and the label its memories and plans
    get when a push names none (the harness source's, from knowledge.yaml)."""

    def __init__(
        self,
        name: str,
        levels: Sequence[str],
        locations: Sequence[str],
        sinks: Sequence[Mapping],
        default_label: Mapping | None = None,
    ):
        sinks = [dict(sink) for sink in sinks]
        knowledge = {"policy": {"levels": list(levels), "locations": list(locations), "sinks": sinks}}
        self.name = name
        self.policy = Policy(name, knowledge)
        self.levels = self.policy.levels
        self.locations = self.policy.locations
        # Policy reads a clearance level it does not know as the highest; here such a sink admits nothing.
        self._closed = {sink["id"] for sink in sinks if not self._known_clearance(sink.get("clearance"))}
        self.hub_sink: Clearance | None = next((c for c in self.policy.sinks.values() if c.kind == HUB_KIND), None)
        self.default_label = dict(default_label) if default_label else self.describe(self.top())

    def _known_clearance(self, clearance) -> bool:
        if not isinstance(clearance, Mapping) or _rank(self.levels, clearance.get("level")) is None:
            return False
        return clearance.get("location") is None or _rank(self.locations, clearance["location"]) is not None

    def top(self) -> Label:
        """The highest label of the project: what an object without a readable label is treated as."""
        return Label(len(self.levels) - 1, len(self.locations) - 1, UNTRUSTED, frozenset({self.name}))

    def describe(self, label: Label) -> dict:
        """``label`` by names, as the hub stores it."""
        return {**self.policy.describe(label), "projects": sorted(label.projects)}

    def stored(self, data) -> Label | None:
        """The label of a stored object, for reading. A level or location the ladder lacks reads as the highest;
        without a location the object is unrestricted, as for a source in knowledge.yaml; without projects it
        belongs to this project. None when the label cannot be read at all, which hides the object."""
        if not isinstance(data, Mapping):
            return None
        projects = data.get("projects", [self.name])
        if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
            return None
        level = _rank(self.levels, data.get("level"))
        location = 0 if data.get("location") is None else _rank(self.locations, data["location"])
        return Label(
            len(self.levels) - 1 if level is None else level,
            len(self.locations) - 1 if location is None else location,
            "T" if data.get("integrity") == "T" else UNTRUSTED,
            frozenset(projects),
        )

    def given(self, data) -> Label:
        """The label a push names, read strictly: a name the ladder lacks is refused with 422, never guessed."""
        if not isinstance(data, Mapping):
            raise Refused(422, "a label must be an object {level, location, integrity, projects}")
        level = _rank(self.levels, data.get("level"))
        if level is None:
            levels = ", ".join(self.levels)
            raise Refused(422, f"label level {data.get('level')!r} is not a level of project {self.name} ({levels})")
        location = 0 if data.get("location") is None else _rank(self.locations, data["location"])
        if location is None:
            locations = ", ".join(self.locations)
            raise Refused(
                422, f"label location {data['location']!r} is not a location of project {self.name} ({locations})"
            )
        integrity = data.get("integrity", UNTRUSTED)
        if integrity not in INTEGRITIES:
            raise Refused(422, f"label integrity must be T or U, not {integrity!r}")
        projects = data.get("projects", [self.name])
        if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
            raise Refused(422, "label projects must be a list of project names")
        return Label(level, location, integrity, frozenset(projects))

    def _as_label(self, clearance: Clearance) -> Label:
        return Label(clearance.level, clearance.location, UNTRUSTED, frozenset({self.name}))

    def grant_label(self, max_level: str | None) -> Label | None:
        """The label of a grant reaching ``max_level``: that level, every location, either integrity, this project.
        None without a grant or for a level the ladder lacks."""
        level = _rank(self.levels, max_level)
        if level is None:
            return None
        return Label(level, len(self.locations) - 1, UNTRUSTED, frozenset({self.name}))

    def ceiling(self, max_level: str | None, sink: str) -> Label | None:
        """meet(label of ``max_level``, clearance of ``sink``): the highest label a member whose grant reaches
        ``max_level`` may read through ``sink``. None when nothing passes: no grant, or a max level or sink the
        project does not declare."""
        grant = self.grant_label(max_level)
        if grant is None or sink not in self.policy.sinks or sink in self._closed:
            return None
        return grant.meet(self._as_label(self.policy.clearance(sink)))

    def visible(self, label, max_level: str | None, sink: str) -> bool:
        """The read rule: may a member whose grant reaches ``max_level`` see, through ``sink``, an object
        carrying ``label`` (by names, as stored)?"""
        ceiling = self.ceiling(max_level, sink)
        found = self.stored(label)
        return ceiling is not None and found is not None and found.below(ceiling)

    def visible_by_grant(self, label, max_level: str | None) -> bool:
        """The read rule when no sink takes part: may a member whose grant reaches ``max_level`` see an object
        carrying ``label`` (by names, as stored) on the hub itself, as the web shows it to them? The label must be
        below the grant's label: its level at most ``max_level`` and its projects this one alone. No sink narrows
        it, so it lets through at least what ``visible`` lets through any sink, never more than the grant. Fails
        closed as ``visible`` does: no grant, a level the ladder lacks, or a label that cannot be read shows
        nothing."""
        grant = self.grant_label(max_level)
        found = self.stored(label)
        return grant is not None and found is not None and found.below(grant)

    def no_hub_sink(self) -> str:
        """Why a project without a sink of kind hub takes no push, and the sink to declare: one that clears the
        default label, so memories and plans without a label of their own can go."""
        level = self.default_label.get("level") or self.levels[0]
        location = self.default_label.get("location")
        example = hub_sink_example(level, None if location in (None, self.locations[0]) else location)
        return (
            f"project {self.name} declares no sink of kind hub, so nothing may be pushed to it: declare one in "
            f"policy.sinks of its knowledge.yaml, such as {example}, then run `evo-agents hub project register` "
            "from its harness"
        )

    def check_push(self, label, role: str | None) -> dict:
        """The write rule. The label to store a pushed object with (``label``, or the project's default when it is
        None), by names; raises Refused when the push must not happen."""
        if not has_role(role, "writer"):
            raise Refused(403, f"pushing to project {self.name} needs the writer role on it")
        if self.hub_sink is None:
            raise Refused(422, self.no_hub_sink())
        if self.hub_sink.sink in self._closed:
            raise Refused(
                422,
                f"hub sink {self.hub_sink.sink!r} of project {self.name} clears a level or location its ladder lacks: "
                "fix it in knowledge.yaml and run `evo-agents hub project register` again",
            )
        found = self.given(self.default_label if label is None else label)
        clearance = self._as_label(self.hub_sink)
        if not found.below(clearance):
            named = self.describe(found)
            cleared = self.describe(clearance)
            outside = sorted(found.projects - {self.name})
            reason = (
                f"it names other projects ({', '.join(outside)})"
                if outside
                else f"{named['level']}/{named['location']} is above {cleared['level']}/{cleared['location']}"
            )
            raise Refused(
                422,
                f"the label is not cleared by hub sink {self.hub_sink.sink!r} of project {self.name}: {reason}. "
                "Nothing was written; raise the sink's clearance in knowledge.yaml and register the project again "
                "if this level may leave the machine",
            )
        return self.describe(found)
