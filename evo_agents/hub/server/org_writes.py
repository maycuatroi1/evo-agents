"""The writes of the org chart (plan org-chart, criterion a3): units created, edited and archived, seats added, edited
and ended, and ceilings set, each by the rules of ``evo_agents.hub.org`` (403 naming why otherwise).

Every write holds ``org_store.lock`` until its transaction ends and reads the chart under it, so two writes at once
apply one after the other and neither breaks what the other checked: one root, no cycle, one live primary seat a
person. A write raises the revision of the unit it changes, or of the unit of the seat it changes, by one, or adds a
ceiling revision, and adds one audit row of an ``org.`` action naming the actor, the unit or seat, and each field's
value before and after it. Units and seats are never deleted: a unit is archived, a seat ended.

A seat made primary takes the place of the person's primary seat before it, which stays live as a secondary seat. The
writer must be one who may edit that seat too, unless it lies at the root, where everyone starts: a lead pulls a person
from the root into their unit, never out of another lead's unit. A primary seat is never made secondary directly: the
person would fall back to the root's level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.org_models import CeilingSet, CeilingWritten, SeatAdd, SeatEdit, UnitCreate, UnitEdit
from evo_agents.hub.server.org_store import load, lock, profile_by_handle, today, user_by_login

log = logging.getLogger(__name__)


@dataclass
class Write:
    """One write of the chart: the connection, who writes, the chart under the lock and the day."""

    conn: AsyncConnection
    actor: org.Actor
    token_id: int | None
    chart: org.Chart
    today: date

    def unit(self, slug: str) -> org.Unit:
        found = self.chart.unit_by_slug(slug)
        if found is None:
            raise HTTPException(404, f"the org chart has no unit {slug}")
        return found

    def seat(self, seat_id: int) -> org.Seat:
        found = self.chart.seats.get(seat_id)
        if found is None:
            raise HTTPException(404, f"the org chart has no seat {seat_id}")
        return found

    def slug(self, unit_id: int) -> str:
        return self.chart.units[unit_id].slug


async def begin(conn: AsyncConnection, actor: org.Actor, token_id: int | None) -> Write:
    """Take the chart's lock and read the chart under it."""
    await lock(conn)
    return Write(conn, actor, token_id, await load(conn), await today(conn))


def forbid(reason: str | None) -> None:
    if reason is not None:
        raise HTTPException(403, reason)


def _changed(name: str, before, after) -> str:
    def shown(value) -> str:
        return "none" if value is None else str(value).lower() if isinstance(value, bool) else str(value)

    return f"{name}={shown(before)}->{shown(after)}"


async def _record(w: Write, action: str, target: str) -> None:
    await audit.record(w.conn, actor_id=w.actor.user_id, token_id=w.token_id, action=action, target=target)
    log.info("org chart changed", extra={"action": action, "login": w.actor.login})


async def _bump(w: Write, unit_id: int) -> int:
    """Raise the unit's revision by one; the revision now."""
    u = tables.org_units
    raised = await w.conn.execute(
        update(u)
        .values(revision=u.c.revision + 1, updated_at=func.now())
        .where(u.c.id == unit_id)
        .returning(u.c.revision)
    )
    return raised.scalar_one()


async def _person(w: Write, login: str) -> tuple[int, str]:
    found = await user_by_login(w.conn, login)
    if found is None:
        raise HTTPException(404, f"{login} has never signed in to the hub nor been granted a project")
    return found.id, found.login


# Units


async def create_unit(w: Write, body: UnitCreate) -> str:
    """A unit below ``body.parent``, with its ceiling: ``body.level``, A1 for a cohort without one; its slug."""
    parent = w.unit(body.parent)
    forbid(org.create_refusal(w.chart, w.actor, parent, w.today))
    if parent.archived_at is not None:
        raise HTTPException(409, f"unit {parent.slug} is archived; nothing changed")
    if w.chart.unit_by_slug(body.slug) is not None:
        raise HTTPException(409, f"the org chart has a unit {body.slug} already; nothing changed")
    if body.ends_on is not None and body.kind != org.COHORT:
        raise HTTPException(422, "ends_on is a cohort's alone; nothing changed")
    level = body.level or (org.COHORT_LEVEL if body.kind == org.COHORT else None)
    if level is not None:
        forbid(org.level_refusal(w.chart, w.actor, level, w.today))
    u = tables.org_units
    values = {
        "parent_id": parent.id,
        "slug": body.slug,
        "name": body.name or body.slug,
        "kind": body.kind,
        "ends_on": body.ends_on,
        "created_by": w.actor.user_id,
    }
    unit_id = (await w.conn.execute(insert(u).values(values).returning(u.c.id))).scalar_one()
    ends = "" if body.ends_on is None else f" ends_on={body.ends_on}"
    await _record(w, audit.ORG_UNIT_CREATE, f"unit:{body.slug} parent={parent.slug} kind={body.kind}{ends} revision=1")
    if level is not None:
        await write_ceiling(w, org.UNIT, unit_id, f"unit:{body.slug}", None, level, body.note)
    return body.slug


async def edit_unit(w: Write, slug: str, body: UnitEdit) -> str:
    """Change the name, the parent or the end date of a unit; its slug."""
    unit = w.unit(slug)
    forbid(org.unit_refusal(w.chart, w.actor, unit, w.today, what=f"editing unit {slug}"))
    if body.if_revision is not None and body.if_revision != unit.revision:
        raise HTTPException(409, f"unit {slug} is at revision {unit.revision}, not {body.if_revision}; nothing changed")
    given = body.model_fields_set
    if ("name" in given and body.name is None) or ("parent" in given and body.parent is None):
        raise HTTPException(422, "a unit always has a name and a parent; nothing changed")
    after: dict = {}
    if body.name is not None:
        after["name"] = body.name
    if body.parent is not None:
        parent = w.unit(body.parent)
        if parent.id != unit.parent_id:
            problem = org.parent_problem(w.chart, unit, parent)
            if problem is not None:
                raise HTTPException(409, f"{problem}; nothing changed")
            forbid(org.create_refusal(w.chart, w.actor, parent, w.today))
        after["parent_id"] = parent.id
    if "ends_on" in given:
        if unit.kind != org.COHORT and body.ends_on is not None:
            raise HTTPException(422, "ends_on is a cohort's alone; nothing changed")
        after["ends_on"] = body.ends_on
    before = {"name": unit.name, "parent_id": unit.parent_id, "ends_on": unit.ends_on}
    values = {name: value for name, value in after.items() if value != before[name]}
    shown = {name: w.slug(value) if name == "parent_id" and value else value for name, value in before.items()}
    changes = [
        _changed(name.removesuffix("_id"), shown[name], w.slug(value) if name == "parent_id" else value)
        for name, value in values.items()
    ]
    if not values:
        return slug
    u = tables.org_units
    await w.conn.execute(update(u).values(values).where(u.c.id == unit.id))
    revision = await _bump(w, unit.id)
    await _record(w, audit.ORG_UNIT_EDIT, f"unit:{slug} {' '.join(changes)} revision={revision}")
    return slug


async def archive_unit(w: Write, slug: str) -> str:
    """Archive a unit holding no live unit or seat; its slug."""
    unit = w.unit(slug)
    forbid(org.unit_refusal(w.chart, w.actor, unit, w.today, what=f"archiving unit {slug}"))
    problem = org.archive_problem(w.chart, unit, w.today)
    if problem is not None:
        raise HTTPException(409, f"{problem}; nothing changed")
    u = tables.org_units
    await w.conn.execute(update(u).values(archived_at=func.now()).where(u.c.id == unit.id))
    revision = await _bump(w, unit.id)
    await _record(w, audit.ORG_UNIT_ARCHIVE, f"unit:{slug} archived=none->now revision={revision}")
    return slug


# Seats


@dataclass(frozen=True)
class Holder:
    user_id: int | None
    profile_id: int | None
    label: str  # "login=<l>" or "agent=<project>/<handle>"


async def _holder(w: Write, body: SeatAdd) -> Holder:
    """Who the new seat is for, once its role fits them."""
    if body.agent is not None or body.project is not None:
        if body.agent is None or body.project is None or body.login is not None:
            raise HTTPException(422, "an agent's seat names agent and project, and no login; nothing changed")
        if body.role != org.AGENT or body.sponsor is None:
            raise HTTPException(422, "an agent's seat has role agent and a sponsor; nothing changed")
        found = await profile_by_handle(w.conn, body.agent, body.project)
        if found is None:
            raise HTTPException(404, f"project {body.project} has no agent profile {body.agent}")
        if [s for s in w.chart.seats.values() if s.profile_id == found.id and not w.chart.ended(s, w.today)]:
            raise HTTPException(409, f"agent {body.agent} holds a live seat already; nothing changed")
        return Holder(None, found.id, f"agent={found.project}/{found.handle}")
    if body.login is None:
        raise HTTPException(422, "a seat names a login, or an agent and its project; nothing changed")
    if body.role not in org.PERSON_ROLES:
        raise HTTPException(422, f"role agent is an agent profile's; a person's seat is one of {org.PERSON_ROLES}")
    user_id, login = await _person(w, body.login)
    return Holder(user_id, None, f"login={login}")


async def _reports_to(w: Write, login: str | None, seat_user_id: int | None) -> int | None:
    """The seat ``login`` holds that a seat reports to: their live primary seat, else a live one; None for None."""
    if login is None:
        return None
    user_id, shown = await _person(w, login)
    live = [s for s in w.chart.seats_of_user(user_id) if not w.chart.ended(s, w.today)]
    if not live:
        raise HTTPException(422, f"{shown} holds no live seat to report to; nothing changed")
    target = next((s for s in live if s.primary), live[0])
    problem = org.reports_to_problem(w.chart, seat_user_id, target, w.today)
    if problem is not None:
        raise HTTPException(422, f"{problem}; nothing changed")
    return target.id


def _dates(starts_on: date, ends_on: date | None) -> None:
    if ends_on is not None and ends_on < starts_on:
        raise HTTPException(422, f"ends_on {ends_on} comes before starts_on {starts_on}; nothing changed")


async def _take_primary(w: Write, user_id: int, keep: int | None) -> None:
    """Make way for a new primary seat of the person: their live primary seat, but ``keep``, becomes secondary,
    once the writer may edit it or it lies at the root."""
    previous = w.chart.primary_seat(user_id, w.today)
    if previous is None or previous.id == keep:
        return
    unit = w.chart.units[previous.unit_id]
    if unit.id != w.chart.root.id:
        what = f"moving the primary seat {previous.id} out of {unit.slug}"
        forbid(org.seat_refusal(w.chart, w.actor, unit, previous.role, w.today, what=what))
    if w.chart.ended(previous, w.today):
        return
    s = tables.org_seats
    await w.conn.execute(update(s).values(is_primary=False, updated_at=func.now()).where(s.c.id == previous.id))
    revision = await _bump(w, unit.id)
    target = f"seat:{previous.id} unit={unit.slug} {_changed('primary', True, False)} revision={revision}"
    await _record(w, audit.ORG_SEAT_EDIT, target)


def _primary_default(w: Write, holder: Holder, asked: bool | None) -> bool:
    if holder.profile_id is not None:
        return True
    if asked is not None:
        return asked
    previous = w.chart.primary_seat(holder.user_id, w.today)
    return previous is None or previous.unit_id == w.chart.root.id


async def add_seat(w: Write, body: SeatAdd) -> int:
    """A seat for a person or an agent profile in a unit; its id."""
    unit = w.unit(body.unit)
    if unit.archived_at is not None:
        raise HTTPException(409, f"unit {unit.slug} is archived; nothing changed")
    holder = await _holder(w, body)
    forbid(org.seat_refusal(w.chart, w.actor, unit, body.role, w.today, what="adding a seat"))
    reports_to = await _reports_to(w, body.reports_to, holder.user_id)
    if body.role == org.INTERN and reports_to is None:
        raise HTTPException(422, "an intern's seat reports to a person: name reports_to; nothing changed")
    sponsor_id = None if body.sponsor is None else (await _person(w, body.sponsor))[0]
    starts_on = body.starts_on or w.today
    _dates(starts_on, body.ends_on)
    if holder.user_id is not None:
        held = [s for s in w.chart.seats_of_user(holder.user_id) if s.unit_id == unit.id]
        if [s for s in held if not w.chart.ended(s, w.today)]:
            raise HTTPException(409, f"{holder.label} holds a live seat in {unit.slug} already; nothing changed")
    primary = _primary_default(w, holder, body.primary)
    if primary and holder.user_id is not None:
        await _take_primary(w, holder.user_id, None)
    s = tables.org_seats
    values = {
        "unit_id": unit.id,
        "user_id": holder.user_id,
        "profile_id": holder.profile_id,
        "role": body.role,
        "is_primary": primary,
        "reports_to": reports_to,
        "sponsor_id": sponsor_id,
        "starts_on": starts_on,
        "ends_on": body.ends_on,
        "created_by": w.actor.user_id,
    }
    seat_id = (await w.conn.execute(insert(s).values(values).returning(s.c.id))).scalar_one()
    revision = await _bump(w, unit.id)
    shown = f"role={body.role} primary={str(primary).lower()}"
    await _record(w, audit.ORG_SEAT_ADD, f"seat:{seat_id} unit={unit.slug} {holder.label} {shown} revision={revision}")
    return seat_id


async def _edited_role(w: Write, seat: org.Seat, unit: org.Unit, role: str | None) -> str:
    if role is None or role == seat.role:
        return seat.role
    if seat.profile_id is not None or role == org.AGENT:
        raise HTTPException(422, "role agent is an agent profile's seat alone, and theirs never changes")
    forbid(org.seat_refusal(w.chart, w.actor, unit, role, w.today, what=f"making seat {seat.id}"))
    return role


async def edit_seat(w: Write, seat_id: int, body: SeatEdit) -> int:
    """Change the role, reports_to, sponsor or dates of a live seat, or make it primary; its id."""
    seat = w.seat(seat_id)
    if w.chart.ended(seat, w.today):
        raise HTTPException(409, f"seat {seat_id} has ended; nothing changed")
    unit = w.chart.units[seat.unit_id]
    forbid(org.seat_refusal(w.chart, w.actor, unit, seat.role, w.today, what=f"editing seat {seat_id}"))
    given = body.model_fields_set
    if ("role" in given and body.role is None) or ("starts_on" in given and body.starts_on is None):
        raise HTTPException(422, "a seat always has a role and a start date; nothing changed")
    after = {"role": await _edited_role(w, seat, unit, body.role)}
    if "reports_to" in given:
        after["reports_to"] = await _reports_to(w, body.reports_to, seat.user_id)
    if "sponsor" in given:
        if body.sponsor is None and seat.profile_id is not None:
            raise HTTPException(422, "an agent's seat always has a sponsor; nothing changed")
        after["sponsor_id"] = None if body.sponsor is None else (await _person(w, body.sponsor))[0]
    if body.starts_on is not None:
        after["starts_on"] = body.starts_on
    if "ends_on" in given:
        after["ends_on"] = body.ends_on
    before = {"role": seat.role, "reports_to": seat.reports_to, "sponsor_id": seat.sponsor_id}
    before |= {"starts_on": seat.starts_on, "ends_on": seat.ends_on}
    if after["role"] == org.INTERN and after.get("reports_to", seat.reports_to) is None:
        raise HTTPException(422, "an intern's seat reports to a person: name reports_to; nothing changed")
    _dates(after.get("starts_on", seat.starts_on), after.get("ends_on", seat.ends_on))
    values = {name: value for name, value in after.items() if value != before[name]}
    changes = [_changed(name.removesuffix("_id"), before[name], value) for name, value in values.items()]
    if body.primary and not seat.primary and seat.user_id is not None:
        await _take_primary(w, seat.user_id, seat.id)
        values["is_primary"] = True
        changes.append(_changed("primary", False, True))
    if not values:
        return seat_id
    s = tables.org_seats
    await w.conn.execute(update(s).values({**values, "updated_at": func.now()}).where(s.c.id == seat_id))
    revision = await _bump(w, unit.id)
    await _record(w, audit.ORG_SEAT_EDIT, f"seat:{seat_id} unit={unit.slug} {' '.join(changes)} revision={revision}")
    return seat_id


async def end_seat(w: Write, seat_id: int) -> int:
    """End a live seat now; its id."""
    seat = w.seat(seat_id)
    if seat.ended_at is not None:
        raise HTTPException(409, f"seat {seat_id} has ended already; nothing changed")
    unit = w.chart.units[seat.unit_id]
    forbid(org.seat_refusal(w.chart, w.actor, unit, seat.role, w.today, what=f"ending seat {seat_id}"))
    s = tables.org_seats
    await w.conn.execute(update(s).values(ended_at=func.now(), updated_at=func.now()).where(s.c.id == seat_id))
    revision = await _bump(w, unit.id)
    await _record(w, audit.ORG_SEAT_END, f"seat:{seat_id} unit={unit.slug} ended=none->now revision={revision}")
    return seat_id


# Ceilings


async def write_ceiling(
    w: Write, scope: str, scope_id: int, target: str, previous: str | None, level: str, note: str | None
) -> CeilingWritten:
    """The next ceiling revision of the unit or seat, with its audit row; the caller checked who writes it."""
    c = tables.autonomy_ceilings
    held = select(func.coalesce(func.max(c.c.revision), 0)).where(c.c.scope == scope, c.c.scope_id == scope_id)
    revision = (await w.conn.execute(held)).scalar_one() + 1
    note = " ".join((note or "").split()) or None
    values = {"scope": scope, "scope_id": scope_id, "revision": revision, "level": level, "note": note}
    await w.conn.execute(insert(c).values({**values, "author_id": w.actor.user_id}))
    await _record(
        w, audit.ORG_CEILING_SET, f"ceiling {target} {_changed('level', previous, level)} revision={revision}"
    )
    return CeilingWritten(scope=scope, target=target, revision=revision, level=level, previous=previous, note=note)


async def set_ceiling(w: Write, body: CeilingSet) -> CeilingWritten:
    """The next ceiling revision of a unit, of a person's live primary seat or of a seat."""
    named = [given for given in (body.unit, body.login, body.seat) if given is not None]
    if len(named) != 1:
        raise HTTPException(422, "a ceiling names exactly one of unit, login and seat; nothing changed")
    if body.unit is not None:
        unit = w.unit(body.unit)
        if unit.archived_at is not None:
            raise HTTPException(409, f"unit {unit.slug} is archived; nothing changed")
        forbid(org.ceiling_refusal(w.chart, w.actor, org.UNIT, unit, body.level, w.today))
        return await write_ceiling(w, org.UNIT, unit.id, f"unit:{unit.slug}", unit.ceiling, body.level, body.note)
    if body.login is not None:
        user_id, login = await _person(w, body.login)
        seat = w.chart.primary_seat(user_id, w.today)
        if seat is None or w.chart.ended(seat, w.today):
            raise HTTPException(409, f"{login} holds no live primary seat; nothing changed")
    else:
        seat = w.seat(body.seat)
        if w.chart.ended(seat, w.today):
            raise HTTPException(409, f"seat {seat.id} has ended; nothing changed")
    forbid(org.ceiling_refusal(w.chart, w.actor, org.SEAT, seat, body.level, w.today))
    return await write_ceiling(w, org.SEAT, seat.id, f"seat:{seat.id}", seat.ceiling, body.level, body.note)
