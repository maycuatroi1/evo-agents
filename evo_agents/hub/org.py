"""The org chart of the hub (plan org-chart, criteria a1 to a3): a tree of units with one root, the seats people and
agent profiles hold in them, and the autonomy ceiling, a level from A0 to A5, each unit and seat may carry. Tables
org_units, org_seats and autonomy_ceilings (``evo_agents.hub.tables_org``, schema 0099); the routes and the writes are
``evo_agents.hub.server.org`` and ``evo_agents.hub.server.org_store``.

The effective ceiling of a person or an agent is the lowest level among the ceiling of their primary seat, that of
the unit holding it and those of every unit above it up to the root (``effective``), with its source: the unit or the
seat that sets it, the one nearest the seat when two set the same level. A primary seat that ended gives A0, its
source the seat itself; a person holding no seat at all counts as a member of the root. A unit or a seat without a
ceiling leaves the level to those above it; the root always has one (A5 from schema 0099 on).

Who may change what (``refusal``), without escalation: a hub admin changes everything, the root and its ceiling
included. The lead of a unit (a live seat of role lead there) creates units below it; adds, edits and ends the member
and intern seats of the unit and every seat of the units below it; and sets the ceiling of those units and seats. A
lead never sets a ceiling above their own effective level, never the ceiling of the unit they lead or of a seat of
their own, and never adds a lead to the unit they lead. Everyone else is refused, and told why.

This module holds the rules as pure functions on a ``Chart``, a snapshot of the tables; it imports nothing of the
server, so the CLI and the web contract may read its names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime

LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")
LOWEST, HIGHEST = LEVELS[0], LEVELS[-1]
COHORT_LEVEL = "A1"  # a cohort created without a level gets this ceiling (the member's answer 3 of 2026-10-22)
ROOT_SLUG = "root"
ROOT_NAME = "Organization"
ORG, TEAM, COHORT = UNIT_KINDS = ("org", "team", "cohort")
CHILD_KINDS = (TEAM, COHORT)  # the root alone is of kind org
LEAD, MEMBER, INTERN, AGENT = ROLES = ("lead", "member", "intern", "agent")
PERSON_ROLES = (LEAD, MEMBER, INTERN)
UNIT, SEAT = SCOPES = ("unit", "seat")  # autonomy_ceilings.scope
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
SLUG = re.compile(SLUG_PATTERN[1:-1])
MAX_NAME_CHARS = 200
MAX_NOTE_CHARS = 1000


def rank(level: str) -> int:
    """The place of ``level`` on the ladder, A0 being 0."""
    return LEVELS.index(level)


@dataclass(frozen=True)
class Unit:
    id: int
    parent_id: int | None
    slug: str
    name: str
    kind: str
    ends_on: date | None = None
    archived_at: datetime | None = None
    revision: int = 1
    ceiling: str | None = None  # the level of its newest ceiling revision; None without one


@dataclass(frozen=True)
class Seat:
    id: int
    unit_id: int
    role: str
    user_id: int | None = None
    profile_id: int | None = None
    primary: bool = False
    reports_to: int | None = None  # a seat of a person
    sponsor_id: int | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    ended_at: datetime | None = None
    ceiling: str | None = None


@dataclass(frozen=True)
class Effective:
    level: str
    source: str  # "unit:<slug>" or "seat:<id>"


@dataclass
class Chart:
    """The org chart as the tables hold it now: units and seats by id."""

    units: dict[int, Unit]
    seats: dict[int, Seat] = field(default_factory=dict)

    @property
    def root(self) -> Unit:
        roots = [unit for unit in self.units.values() if unit.parent_id is None]
        if len(roots) != 1:
            raise ValueError(f"the org chart has {len(roots)} roots, not one")
        return roots[0]

    def unit_by_slug(self, slug: str) -> Unit | None:
        return next((unit for unit in self.units.values() if unit.slug == slug), None)

    def ancestors(self, unit_id: int) -> Iterator[Unit]:
        """The unit and every unit above it, up to the root; stops at a unit seen twice, which no write lets in."""
        seen: set[int] = set()
        current = self.units.get(unit_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            yield current
            current = None if current.parent_id is None else self.units.get(current.parent_id)

    def is_below(self, unit_id: int, above_id: int) -> bool:
        """Whether ``unit_id`` lies strictly below ``above_id``."""
        return any(unit.id == above_id for unit in self.ancestors(unit_id) if unit.id != unit_id)

    def within(self, unit_id: int, top_id: int) -> bool:
        """Whether ``unit_id`` is ``top_id`` or lies below it."""
        return unit_id == top_id or self.is_below(unit_id, top_id)

    def children(self, unit_id: int) -> list[Unit]:
        return [unit for unit in self.units.values() if unit.parent_id == unit_id]

    def seats_of_user(self, user_id: int) -> list[Seat]:
        return [seat for seat in self.seats.values() if seat.user_id == user_id]

    def primary_seat(self, user_id: int, today: date) -> Seat | None:
        """The seat that decides the person's ceiling: their live primary seat, else the primary seat that ended last;
        None when they never held one."""
        primaries = [seat for seat in self.seats_of_user(user_id) if seat.primary]
        live = [seat for seat in primaries if not self.ended(seat, today)]
        if live:
            return live[0]
        return max(primaries, key=lambda seat: (seat.ended_at or datetime.min, seat.id), default=None)

    def ended(self, seat: Seat, today: date) -> bool:
        """Whether the seat ended: ended_at set, or its own end date or that of a cohort holding it passed."""
        if seat.ended_at is not None or (seat.ends_on is not None and seat.ends_on < today):
            return True
        return any(
            unit.kind == COHORT and unit.ends_on is not None and unit.ends_on < today
            for unit in self.ancestors(seat.unit_id)
        )

    def live_leads(self, user_id: int, today: date) -> list[int]:
        """The units the person leads: those holding a live seat of role lead of theirs."""
        return [
            seat.unit_id for seat in self.seats_of_user(user_id) if seat.role == LEAD and not self.ended(seat, today)
        ]


# The effective ceiling


def lowest(chain: Iterable[tuple[str, str | None]]) -> Effective:
    """The lowest level of ``chain``, (source, level or None) from the seat up to the root, with its source; the
    nearest of those that set the same level. A chain that sets none is A5, its source the last of it."""
    found: Effective | None = None
    last = ""
    for source, level in chain:
        last = source
        if level is not None and (found is None or rank(level) < rank(found.level)):
            found = Effective(level, source)
    return found if found is not None else Effective(HIGHEST, last)


def unit_chain(chart: Chart, unit_id: int) -> list[tuple[str, str | None]]:
    return [(f"unit:{unit.slug}", unit.ceiling) for unit in chart.ancestors(unit_id)]


def unit_effective(chart: Chart, unit_id: int) -> Effective:
    """The lowest ceiling of the unit and the units above it."""
    return lowest(unit_chain(chart, unit_id))


def seat_effective(chart: Chart, seat: Seat, today: date) -> Effective:
    """The level a seat gives: A0 once it ended, else the lowest ceiling of it and its units."""
    if chart.ended(seat, today):
        return Effective(LOWEST, f"seat:{seat.id}")
    return lowest([(f"seat:{seat.id}", seat.ceiling), *unit_chain(chart, seat.unit_id)])


def effective(chart: Chart, user_id: int, today: date) -> Effective:
    """The effective ceiling of a person: that of their primary seat, A0 when it ended, the root's without one."""
    seat = chart.primary_seat(user_id, today)
    if seat is None:
        return unit_effective(chart, chart.root.id)
    return seat_effective(chart, seat, today)


def profile_effective(chart: Chart, profile_id: int, today: date) -> Effective | None:
    """The effective ceiling of an agent profile's live seat; None when it holds none (it acts for whoever runs it)."""
    for seat in chart.seats.values():
        if seat.profile_id == profile_id and not chart.ended(seat, today):
            return seat_effective(chart, seat, today)
    return None


# Seats that ended (criterion a5)


def end_day(chart: Chart, seat: Seat, today: date) -> date | None:
    """The day the seat ended: the earliest of the day of its ended_at and of the end dates, its own and those of the
    cohorts holding it, that passed; None while it is live."""
    days = [seat.ends_on] if seat.ends_on is not None and seat.ends_on < today else []
    days += [
        unit.ends_on
        for unit in chart.ancestors(seat.unit_id)
        if unit.kind == COHORT and unit.ends_on is not None and unit.ends_on < today
    ]
    if seat.ended_at is not None:
        days.append(seat.ended_at.date())
    return min(days, default=None)


def overdue(chart: Chart, today: date) -> list[Seat]:
    """The seats past their end date or that of a cohort holding them whose ended_at is not set yet."""
    return [seat for seat in chart.seats.values() if seat.ended_at is None and chart.ended(seat, today)]


def ended_primary(chart: Chart, user_id: int, today: date) -> Seat | None:
    """The person's primary seat when it ended, which puts them at A0; None while they hold a live one or none."""
    seat = chart.primary_seat(user_id, today)
    return seat if seat is not None and chart.ended(seat, today) else None


def out_of_office(chart: Chart, today: date) -> list[int]:
    """The people whose primary seat ended."""
    people = {seat.user_id for seat in chart.seats.values() if seat.user_id is not None}
    return sorted(user_id for user_id in people if ended_primary(chart, user_id, today) is not None)


def ended_refusal(chart: Chart, user_id: int, login: str, today: date, *, what: str) -> str | None:
    """Why ``login`` may not start ``what`` (a run they dispatch or ask of an agent): their primary seat ended, naming
    its unit and the day it ended; None while it is live, or they hold none."""
    seat = ended_primary(chart, user_id, today)
    if seat is None:
        return None
    unit = chart.units[seat.unit_id].slug
    return (
        f"the primary seat of {login} in unit {unit} ended on {end_day(chart, seat, today)}, which leaves them at "
        f"level {LOWEST}: {what} is refused"
    )


# Who may change what


@dataclass(frozen=True)
class Actor:
    user_id: int
    login: str
    admin: bool  # a hub admin, from EVO_HUB_ADMINS


def _led(chart: Chart, actor: Actor, today: date) -> list[Unit]:
    return [chart.units[unit_id] for unit_id in chart.live_leads(actor.user_id, today) if unit_id in chart.units]


def _lead_note(actor: Actor, what: str) -> str:
    return f"{what} needs a hub admin or the lead of a unit above it; {actor.login} is neither"


def unit_refusal(chart: Chart, actor: Actor, unit: Unit, today: date, *, what: str) -> str | None:
    """Why ``actor`` may not edit or archive ``unit`` (it must lie below a unit they lead); None when they may."""
    if actor.admin:
        return None
    if any(chart.is_below(unit.id, led.id) for led in _led(chart, actor, today)):
        return None
    if any(led.id == unit.id for led in _led(chart, actor, today)):
        return f"{what} needs a hub admin or the lead of a unit above it; {actor.login} leads {unit.slug} itself"
    return _lead_note(actor, what)


def create_refusal(chart: Chart, actor: Actor, parent: Unit, today: date) -> str | None:
    """Why ``actor`` may not create a unit below ``parent``; None when they may."""
    if actor.admin or any(chart.within(parent.id, led.id) for led in _led(chart, actor, today)):
        return None
    return f"creating a unit below {parent.slug} needs a hub admin or a lead of {parent.slug} or a unit above it"


def seat_refusal(chart: Chart, actor: Actor, unit: Unit, role: str, today: date, *, what: str) -> str | None:
    """Why ``actor`` may not add, edit or end a seat of ``role`` in ``unit``; None when they may."""
    if actor.admin:
        return None
    led = _led(chart, actor, today)
    if any(chart.is_below(unit.id, top.id) for top in led):
        return None
    if any(top.id == unit.id for top in led):
        if role in (MEMBER, INTERN):
            return None
        return (
            f"{what} of role {role} in {unit.slug} needs a hub admin or the lead of a unit above it; "
            f"a lead of {unit.slug} manages its member and intern seats"
        )
    return f"{what} in {unit.slug} needs a hub admin or a lead of {unit.slug} or a unit above it"


def ceiling_refusal(chart: Chart, actor: Actor, scope: str, target, level: str, today: date) -> str | None:
    """Why ``actor`` may not set ``level`` as the ceiling of ``target`` (a Unit or a Seat); None when they may."""
    if actor.admin:
        return None
    if scope == UNIT:
        refused = unit_refusal(chart, actor, target, today, what=f"setting the ceiling of unit {target.slug}")
    elif target.user_id == actor.user_id:
        refused = f"{actor.login} may not set the ceiling of a seat of their own; a hub admin or a lead above may"
    else:
        unit = chart.units[target.unit_id]
        refused = seat_refusal(chart, actor, unit, target.role, today, what=f"setting the ceiling of seat {target.id}")
    return refused if refused is not None else level_refusal(chart, actor, level, today)


def level_refusal(chart: Chart, actor: Actor, level: str, today: date) -> str | None:
    """Why ``actor`` may not set a ceiling of ``level`` anywhere: it lies above their own effective level."""
    if actor.admin:
        return None
    own = effective(chart, actor.user_id, today)
    if rank(level) > rank(own.level):
        return (
            f"{actor.login} may not set a ceiling above their own effective level {own.level} "
            f"(from {own.source}); {level} asked"
        )
    return None


# Checks of the shape of the tree


def parent_problem(chart: Chart, unit: Unit, parent: Unit) -> str | None:
    """Why ``unit`` may not move below ``parent``; None when it may."""
    if unit.parent_id is None:
        return "the root has no parent"
    if parent.archived_at is not None:
        return f"unit {parent.slug} is archived"
    if chart.within(parent.id, unit.id):
        return f"unit {parent.slug} lies within {unit.slug}: the tree would hold a cycle"
    return None


def archive_problem(chart: Chart, unit: Unit, today: date) -> str | None:
    """Why ``unit`` may not be archived; None when it may."""
    if unit.parent_id is None:
        return "the root is never archived"
    if unit.archived_at is not None:
        return f"unit {unit.slug} is archived already"
    if [child for child in chart.children(unit.id) if child.archived_at is None]:
        return f"unit {unit.slug} still has units below it that are not archived"
    if [seat for seat in chart.seats.values() if seat.unit_id == unit.id and not chart.ended(seat, today)]:
        return f"unit {unit.slug} still has live seats; end them first"
    return None


def reports_to_problem(chart: Chart, seat_user_id: int | None, target: Seat | None, today: date) -> str | None:
    """Why ``target`` may not be the seat a seat of ``seat_user_id`` reports to; None when it may."""
    if target is None:
        return None
    if target.user_id is None:
        return f"seat {target.id} is an agent's: reports_to names a seat of a person"
    if chart.ended(target, today):
        return f"seat {target.id} has ended: reports_to names a live seat"
    if seat_user_id is not None and target.user_id == seat_user_id:
        return "a seat never reports to a seat of the same person"
    return None
