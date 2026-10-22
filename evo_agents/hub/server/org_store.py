"""The org chart as the tables hold it (plan org-chart, criteria a1 and a2): reading it into an ``org.Chart``, the views
GET /v1/org answers, a person's effective ceiling, and the seat every new users row gets.

Every user holds a seat from the moment their users row exists: schema 0099 gave each one a primary member seat at
the root, and ``place_user`` gives one to each row made later (a first sign-in, a grant to a login that never signed
in), in the transaction that makes the row. The writes of the chart are ``evo_agents.hub.server.org_writes``; they all
hold ``lock`` until their transaction ends, so two writes never read the chart the other is changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org, tables
from evo_agents.hub.server.org_models import OrgChartView, OrgSeatView, OrgUnitView, OrgViewer

LOCK_NAME = "org_chart"


async def lock(conn: AsyncConnection) -> None:
    """Hold, until the transaction ends, the lock that serialises every write of the org chart."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(LOCK_NAME, 0))))


def _newest_ceilings():
    """The newest revision of each scope's ceiling."""
    c = tables.autonomy_ceilings
    return (
        select(c.c.scope, c.c.scope_id, c.c.level)
        .ext(distinct_on(c.c.scope, c.c.scope_id))
        .order_by(c.c.scope, c.c.scope_id, c.c.revision.desc())
        .subquery("ceilings")
    )


async def today(conn: AsyncConnection) -> date:
    """The database's current day, which every check of an end date reads."""
    return (await conn.execute(select(func.current_date()))).scalar_one()


async def load(conn: AsyncConnection) -> org.Chart:
    """The whole chart, each unit and seat with the level of its newest ceiling."""
    u, s, newest = tables.org_units, tables.org_seats, _newest_ceilings()
    unit_ceiling = and_(newest.c.scope == org.UNIT, newest.c.scope_id == u.c.id)
    seat_ceiling = and_(newest.c.scope == org.SEAT, newest.c.scope_id == s.c.id)
    unit_rows = await conn.execute(select(u, newest.c.level).outerjoin(newest, unit_ceiling))
    units = {
        row.id: org.Unit(
            id=row.id,
            parent_id=row.parent_id,
            slug=row.slug,
            name=row.name,
            kind=row.kind,
            ends_on=row.ends_on,
            archived_at=row.archived_at,
            revision=row.revision,
            ceiling=row.level,
        )
        for row in unit_rows
    }
    seat_rows = await conn.execute(select(s, newest.c.level).outerjoin(newest, seat_ceiling))
    seats = {
        row.id: org.Seat(
            id=row.id,
            unit_id=row.unit_id,
            role=row.role,
            user_id=row.user_id,
            profile_id=row.profile_id,
            primary=row.is_primary,
            reports_to=row.reports_to,
            sponsor_id=row.sponsor_id,
            starts_on=row.starts_on,
            ends_on=row.ends_on,
            ended_at=row.ended_at,
            ceiling=row.level,
        )
        for row in seat_rows
    }
    return org.Chart(units, seats)


# Who is who


async def user_by_login(conn: AsyncConnection, login: str):
    """(id, login) of the users row of ``login``, case-insensitive; None without one."""
    users = tables.users
    query = select(users.c.id, users.c.login).where(func.lower(users.c.login) == func.lower(login))
    return (await conn.execute(query)).first()


async def profile_by_handle(conn: AsyncConnection, handle: str, project: str):
    """(id, handle, project) of the live agent profile ``handle`` of ``project``; None without one."""
    p, projects = tables.agent_profiles, tables.projects
    query = (
        select(p.c.id, p.c.handle, projects.c.name.label("project"))
        .join_from(p, projects, projects.c.id == p.c.project_id)
        .where(p.c.handle == handle, projects.c.name == project, p.c.deleted_at.is_(None))
    )
    return (await conn.execute(query)).first()


@dataclass(frozen=True)
class Names:
    logins: dict[int, str]
    profiles: dict[int, tuple[str, str]]  # id: (handle, project)


async def names(conn: AsyncConnection, chart: org.Chart) -> Names:
    """The logins of the people and the handles and projects of the profiles the chart's seats name."""
    user_ids = {i for seat in chart.seats.values() for i in (seat.user_id, seat.sponsor_id) if i is not None}
    profile_ids = {seat.profile_id for seat in chart.seats.values() if seat.profile_id is not None}
    users, p, projects = tables.users, tables.agent_profiles, tables.projects
    logins = {}
    if user_ids:
        rows = await conn.execute(select(users.c.id, users.c.login).where(users.c.id.in_(user_ids)))
        logins = {row.id: row.login for row in rows}
    profiles = {}
    if profile_ids:
        query = (
            select(p.c.id, p.c.handle, projects.c.name)
            .join_from(p, projects, projects.c.id == p.c.project_id)
            .where(p.c.id.in_(profile_ids))
        )
        profiles = {row.id: (row.handle, row.name) for row in await conn.execute(query)}
    return Names(logins, profiles)


# Views


def unit_view(chart: org.Chart, unit: org.Unit) -> OrgUnitView:
    reached = org.unit_effective(chart, unit.id)
    parent = None if unit.parent_id is None else chart.units[unit.parent_id].slug
    return OrgUnitView(
        slug=unit.slug,
        name=unit.name,
        kind=unit.kind,
        parent=parent,
        ends_on=unit.ends_on,
        archived_at=unit.archived_at,
        revision=unit.revision,
        ceiling=unit.ceiling,
        effective=reached.level,
        source=reached.source,
    )


def seat_view(chart: org.Chart, seat: org.Seat, known: Names, today: date) -> OrgSeatView:
    reached = org.seat_effective(chart, seat, today)
    above = None if seat.reports_to is None else chart.seats.get(seat.reports_to)
    handle, project = known.profiles.get(seat.profile_id, (None, None)) if seat.profile_id else (None, None)
    return OrgSeatView(
        id=seat.id,
        unit=chart.units[seat.unit_id].slug,
        login=known.logins.get(seat.user_id) if seat.user_id else None,
        agent=handle,
        project=project,
        role=seat.role,
        primary=seat.primary,
        reports_to=known.logins.get(above.user_id) if above is not None and above.user_id else None,
        reports_to_seat=seat.reports_to,
        sponsor=known.logins.get(seat.sponsor_id) if seat.sponsor_id else None,
        starts_on=seat.starts_on,
        ends_on=seat.ends_on,
        ended_at=seat.ended_at,
        ceiling=seat.ceiling,
        effective=reached.level,
        source=reached.source,
    )


async def chart_view(conn: AsyncConnection, actor: org.Actor) -> OrgChartView:
    """The whole chart for ``actor``: every unit and seat with its ceiling and effective level, and the actor's own."""
    chart = await load(conn)
    day = await today(conn)
    known = await names(conn, chart)
    root = chart.root
    units = sorted(chart.units.values(), key=lambda unit: (unit.id != root.id, unit.slug))
    seats = sorted(chart.seats.values(), key=lambda seat: (chart.units[seat.unit_id].slug, seat.id))
    own = org.effective(chart, actor.user_id, day)
    viewer = OrgViewer(
        login=actor.login,
        admin=actor.admin,
        leads=sorted(chart.units[unit_id].slug for unit_id in chart.live_leads(actor.user_id, day)),
        effective=own.level,
        source=own.source,
    )
    return OrgChartView(
        units=[unit_view(chart, unit) for unit in units],
        seats=[seat_view(chart, seat, known, day) for seat in seats],
        viewer=viewer,
    )


async def effective_for_user(conn: AsyncConnection, user_id: int) -> org.Effective:
    """The effective ceiling of a person now (``org.effective``)."""
    return org.effective(await load(conn), user_id, await today(conn))


# The seat of a new users row


def _root_id():
    u = tables.org_units
    return select(u.c.id).where(u.c.parent_id.is_(None)).scalar_subquery()


async def place_user(conn: AsyncConnection, user_id: int) -> None:
    """Give the users row the hub just made its primary member seat at the root, in the caller's transaction."""
    s = tables.org_seats
    seat = {"unit_id": _root_id(), "user_id": user_id, "role": org.MEMBER, "is_primary": True}
    await conn.execute(insert(s).values(seat))


async def merge_user(conn: AsyncConnection, into: int, holder_id: int) -> None:
    """Move the seats of users row ``holder_id``, about to be deleted, to row ``into``, as sign-in merges a login
    granted before its first sign-in into the account that took it: a seat that would be a second live primary one
    stays as a secondary seat, one in a unit where ``into`` holds a live seat already ends, and ``holder_id`` stops
    sponsoring."""
    s = tables.org_seats
    live = s.c.ended_at.is_(None)
    held_units = select(s.c.unit_id).where(s.c.user_id == into, live)
    await conn.execute(
        update(s)
        .values(ended_at=func.now(), updated_at=func.now())
        .where(s.c.user_id == holder_id, live, s.c.unit_id.in_(held_units))
    )
    has_primary = select(s.c.id).where(s.c.user_id == into, live, s.c.is_primary).exists()
    await conn.execute(
        update(s).values(is_primary=False).where(s.c.user_id == holder_id, live, s.c.is_primary, has_primary)
    )
    await conn.execute(update(s).values(user_id=into, updated_at=func.now()).where(s.c.user_id == holder_id))
    await conn.execute(update(s).values(sponsor_id=into).where(s.c.sponsor_id == holder_id))
