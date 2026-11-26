"""The SQL of where a project's night shift stands: its schedules, the newest charters and the open proposals of the
caller's projects, the last review run and brief, the latest nights, and pausing the schedules."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Text, cast, func, literal, select, union, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.curator.repository.charter import zone_sql
from evo_agents.hub.server.night_cost import QUEUED_OR_HELD, usage_cost


def schedules(project_id: int):
    s, owner, pauser = tables.schedules, tables.users.alias("owner"), tables.users.alias("pauser")
    return (
        select(
            s.c.id,
            s.c.kind,
            s.c.owner_id,
            owner.c.login.label("owner"),
            s.c.paused_at,
            pauser.c.login.label("paused_by"),
            s.c.pause_reason,
        )
        .join_from(s, owner, owner.c.id == s.c.owner_id)
        .outerjoin(pauser, pauser.c.id == s.c.paused_by)
        .where(s.c.project_id == project_id)
        .order_by(s.c.kind)
    )


async def schedules_of(conn: AsyncConnection, project_id: int):
    return (await conn.execute(schedules(project_id))).all()


async def local_now(conn: AsyncConnection, zone: str):
    """The time now in the time zone ``zone``."""
    return (await conn.execute(select(func.timezone(zone, func.now())))).scalar_one()


def _newest_charters():
    """The newest charter of each project of the list bound as ``projects``, with the time now in its time zone."""
    c = tables.charters
    local_now = func.timezone(zone_sql(c.c.body), func.now())
    return (
        select(c.c.project_id, c.c.body, local_now.label("local_now"))
        .where(one_of(c.c.project_id, name="projects"))
        .ext(distinct_on(c.c.project_id))
        .order_by(c.c.project_id, c.c.revision.desc())
    )


def _schedules_held():
    """Each schedule of the projects bound as ``projects``: its project, when it was paused, and its first run queued
    or held now (``run_id``, NULL without one)."""
    s, r = tables.schedules, tables.runs
    held = (
        select(func.min(r.c.id)).where(r.c.schedule_id == s.c.id, one_of(r.c.state, QUEUED_OR_HELD)).scalar_subquery()
    )
    return select(s.c.id, s.c.project_id, s.c.paused_at, held.label("run_id")).where(
        one_of(s.c.project_id, name="projects")
    )


def _open_proposal_labels():
    """The label of each open proposal of the projects bound as ``projects``."""
    p = tables.proposals
    return select(p.c.project_id, p.c.label).where(one_of(p.c.project_id, name="projects"), p.c.state == "open")


# Built once with bind parameters: GET /v1/me/overview runs them on every request (docs/hub.md, Data access).
_NEWEST_CHARTERS = _newest_charters()
_SCHEDULES_HELD = _schedules_held()
_OPEN_PROPOSAL_LABELS = _open_proposal_labels()


async def newest_charters(conn: AsyncConnection, project_ids: list[int]):
    return (await conn.execute(_NEWEST_CHARTERS, {"projects": project_ids})).all()


async def schedules_held(conn: AsyncConnection, project_ids: list[int]):
    return (await conn.execute(_SCHEDULES_HELD, {"projects": project_ids})).all()


async def open_proposal_labels(conn: AsyncConnection, project_ids: list[int]):
    return (await conn.execute(_OPEN_PROPOSAL_LABELS, {"projects": project_ids})).all()


async def last_brief(conn: AsyncConnection, project_id: int):
    """The project's last morning brief: id, day, night, created_at, login, notification_id and title."""
    b, u = tables.curator_briefs, tables.users
    query = (
        select(b.c.id, b.c.day, b.c.night, b.c.created_at, u.c.login, b.c.notification_id, b.c.body["title"].astext)
        .join_from(b, u, u.c.id == b.c.user_id)
        .where(b.c.project_id == project_id)
        .order_by(b.c.day.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def last_review(conn: AsyncConnection, project_id: int):
    """The last review run of a night; a review run of the knowledge graph (starter questions, kg_init, an answer
    batch) belongs to no night and is not one."""
    r, f, p, cf = tables.runs, tables.findings, tables.proposals, tables.curator_figures
    findings = select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery()
    proposals = select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery()
    lenses = select(cf.c.figures["lenses"]).where(cf.c.run_id == r.c.id).limit(1).scalar_subquery()
    query = (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            lenses.label("lenses"),
            findings.label("findings"),
            proposals.label("proposals"),
            r.c.queued_at,
            r.c.finished_at,
        )
        .where(r.c.project_id == project_id, r.c.kind == "review", r.c.schedule_night.is_not(None))
        .order_by(r.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


# The latest nights


def _nights_of(project_id: int, limit: int):
    """The latest ``limit`` nights of the project: those its schedules queued runs for, and those curator.collect
    counted figures for."""
    r, s, cf = tables.runs, tables.schedules, tables.curator_figures
    queued = select(r.c.schedule_night.label("night")).where(
        r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)), r.c.schedule_night.is_not(None)
    )
    counted = select(cf.c.night.label("night")).where(cf.c.project_id == project_id)
    nights = union(queued, counted).subquery("nights")
    return select(nights.c.night).order_by(nights.c.night.desc()).limit(limit)


def _night_runs(project_id: int, nights: list[date]):
    """Per night of ``nights``: the runs the project's schedules queued for it, by how they stand."""
    r, s = tables.runs, tables.schedules
    return (
        select(
            r.c.schedule_night.label("night"),
            func.count().label("runs"),
            func.count().filter(r.c.state == "done").label("done"),
            func.count().filter(r.c.state.in_(("failed", "lost"))).label("failed"),
            func.count().filter(r.c.state == "cancelled").label("cancelled"),
            func.count().filter(r.c.state.in_(runs.ACTIVE_STATES)).label("active"),
        )
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night)
    )


def _night_costs(project_id: int, nights: list[date]):
    """Per night of ``nights``: what its runs cost, each agent session once at its largest total, as
    ``_night_figures`` counts one night."""
    r, s = tables.runs, tables.schedules
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(r.c.schedule_night.label("night"), func.max(usage_cost(r.c.usage)).label("cost"))
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night, session)
        .subquery("per_session")
    )
    return select(per_session.c.night, func.sum(per_session.c.cost).label("cost")).group_by(per_session.c.night)


def _night_reviews(project_id: int, nights: list[date]):
    """The review runs of the project for ``nights``, with what each wrote, the latest first."""
    r, f, p = tables.runs, tables.findings, tables.proposals
    return (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery().label("findings"),
            select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery().label("proposals"),
        )
        .where(r.c.project_id == project_id, r.c.kind == "review", r.c.schedule_night.in_(nights))
        .order_by(r.c.id.desc())
    )


async def latest_nights(conn: AsyncConnection, project_id: int, limit: int) -> list[date]:
    return list((await conn.execute(_nights_of(project_id, limit))).scalars())


async def night_runs(conn: AsyncConnection, project_id: int, nights: list[date]) -> dict:
    """Per night of ``nights``, the row of how its runs stand."""
    return {row.night: row for row in (await conn.execute(_night_runs(project_id, nights))).all()}


async def night_costs(conn: AsyncConnection, project_id: int, nights: list[date]) -> dict:
    """Per night of ``nights``, what its runs cost."""
    return {row.night: row.cost for row in (await conn.execute(_night_costs(project_id, nights))).all()}


async def night_reviews(conn: AsyncConnection, project_id: int, nights: list[date]):
    """The review runs of ``nights``, the latest first."""
    return (await conn.execute(_night_reviews(project_id, nights))).all()


async def nights_counted(conn: AsyncConnection, project_id: int, nights: list[date]) -> set[date]:
    """The nights of ``nights`` curator.collect counted figures for."""
    cf = tables.curator_figures
    figures = select(cf.c.night).where(cf.c.project_id == project_id, cf.c.night.in_(nights))
    return set((await conn.execute(figures)).scalars())


# Pausing


async def lock_schedules(conn: AsyncConnection, project_id: int):
    """The id and owner of each schedule of the project, locked, in order of id."""
    s = tables.schedules
    return (
        await conn.execute(
            select(s.c.id, s.c.owner_id).where(s.c.project_id == project_id).order_by(s.c.id).with_for_update()
        )
    ).all()


async def pause(conn: AsyncConnection, ids: list[int], user_id: int) -> None:
    s = tables.schedules
    await conn.execute(
        update(s)
        .values(paused_at=func.now(), paused_by=user_id, updated_at=func.now())
        .where(s.c.id.in_(ids), s.c.paused_at.is_(None))
    )


async def resume(conn: AsyncConnection, ids: list[int]) -> None:
    s = tables.schedules
    await conn.execute(
        update(s)
        .values(paused_at=None, paused_by=None, pause_reason=None, updated_at=func.now())
        .where(s.c.id.in_(ids), s.c.paused_at.is_not(None))
    )
