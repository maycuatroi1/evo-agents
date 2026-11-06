"""What of another project may enter a run: the one check every read across projects and every delegated answer pass
(``docs/hub.md``, Agents across projects).

A run of project P takes an item of project Q (a knowledge graph element, a memory, the answer of an agent of Q) only
when the hub sink of P clears the item's label. The two projects have ladders of their own, so the check goes by
names: a level or location of Q passes when P's ladder has the same name at or below what P's hub sink clears, and
when every lower level (location) of Q passes too, so that the check is one ceiling on Q's ladder (``Gate.ceiling``),
which the graph can read with as it reads with any clearance. A name P's ladder lacks stops the ceiling below it: it
fails closed, as ``evo_agents.hub.access`` does. An item that names projects besides Q, or whose label cannot be read,
never passes; P without a hub sink takes nothing from another project.

``Gate.why`` says why an item does not pass, for the count and the reasons the agent is told (``withheld``); the
items are left out, never shown in part.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub.access import UNTRUSTED, ProjectRules
from evo_agents.kg.policy import TOP_AUDIENCE, Label


def _cleared(source: list[str], target: list[str], cap: int) -> int:
    """The highest index of ``source`` up to which every name is on ``target`` at or below index ``cap``; -1 when
    the lowest is not."""
    top = -1
    for name in source:
        if name not in target or target.index(name) > cap:
            break
        top += 1
    return top


@dataclass(frozen=True)
class Gate:
    """What the hub sink of ``target``, the run's project, lets in of ``source``, the project read."""

    target: ProjectRules
    source: ProjectRules
    ceiling: Label | None  # on the source's ladder; None when nothing passes
    closed: str | None = None  # why nothing passes, when ``ceiling`` is None

    @property
    def sink(self) -> str:
        return self.target.hub_sink.sink if self.target.hub_sink is not None else "-"

    def _cap(self) -> str:
        """What the target's hub sink clears, by names."""
        clearance = self.target.hub_sink
        assert clearance is not None
        level = self.target.levels[clearance.level]
        return f"{level}/{self.target.locations[clearance.location]}"

    def why(self, label) -> str | None:
        """Why an item of the source labelled ``label`` (by names, as stored) does not pass; None when it does."""
        if self.ceiling is None:
            return self.closed
        found = self.source.stored(label)
        if found is None:
            return "its label cannot be read"
        outside = sorted(found.projects - {self.source.name})
        if outside:
            return f"it names other projects ({', '.join(outside)})"
        named = label if isinstance(label, Mapping) else {}
        through = f"what hub sink {self.sink!r} of project {self.target.name} clears ({self._cap()})"
        if found.level > self.ceiling.level:
            return f"level {named.get('level')} of project {self.source.name} is above {through}"
        if found.location > self.ceiling.location:
            where = named.get("location") or self.source.locations[0]
            return f"location {where} of project {self.source.name} is outside {through}"
        return None

    def passes(self, label) -> bool:
        return self.why(label) is None


def gate(target: ProjectRules, source: ProjectRules) -> Gate:
    """The check of what ``target``'s hub sink lets in of ``source`` (see the module)."""
    clearance = target.hub_sink
    if clearance is None or target.ceiling(target.levels[-1], clearance.sink) is None:
        why = f"project {target.name} declares no usable hub sink, so nothing of another project may enter its runs"
        return Gate(target, source, None, why)
    level = _cleared(source.levels, target.levels, clearance.level)
    location = _cleared(source.locations, target.locations, clearance.location)
    if level < 0 or location < 0:
        lowest = source.levels[0] if level < 0 else source.locations[0]
        why = (
            f"hub sink {clearance.sink!r} of project {target.name} clears nothing of project {source.name}: its "
            f"lowest {'level' if level < 0 else 'location'} {lowest} is not one {target.name} lets in"
        )
        return Gate(target, source, None, why)
    ceiling = Label(level, location, UNTRUSTED, frozenset({source.name}), TOP_AUDIENCE)
    return Gate(target, source, ceiling)


def withheld(reasons: Iterable[str]) -> str:
    """One line on the items left out and why, as the agent is told: empty when none was."""
    counted = Counter(reasons)
    total = sum(counted.values())
    if not total:
        return ""
    left = "1 item of what you may read was" if total == 1 else f"{total} items of what you may read were"
    why = "; ".join(f"{count}: {reason}" if len(counted) > 1 else reason for reason, count in counted.most_common())
    return f"{left} left out, since {why}."
