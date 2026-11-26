"""The queries of the weekly report of the Curator's probes: the rows of its weeks, the active probes, the merged
changes without one, the fix proposals and the replacement probes, what a week's probe runs cost, and whether a
week's notice was told."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.probes.models.fixes import OPEN
from evo_agents.hub.server.probes.models.report import MAX_UNPROBED, WEEK


async def week_cost(conn: AsyncConnection, project_id: int, week: date) -> float:
    """What the project's probe runs of the nights of the ISO week of Monday ``week`` cost."""
    r = tables.runs
    found = await conn.execute(
        select(func.coalesce(func.sum(usage_cost(r.c.usage)), 0)).where(
            r.c.project_id == project_id,
            r.c.kind == "probe",
            r.c.schedule_night.between(week, week + WEEK - timedelta(1)),
        )
    )
    return round(float(found.scalar_one() or 0), 6)


async def local_now(conn: AsyncConnection, zone: str) -> datetime:
    """The database's now in time zone ``zone``."""
    return await conn.scalar(select(func.timezone(zone, func.now())))  # pyright: ignore[reportReturnType]


async def week_rows(conn: AsyncConnection, project_id: int, weeks: list[date]) -> list:
    """The project's rows of curator_probe_runs of ``weeks``, each with its probe's label, plugin and rule."""
    pw, pr, pl = tables.curator_probe_runs, tables.curator_probes, tables.plugins
    query = (
        select(pw, pr.c.label.label("probe_label"), pl.c.name.label("plugin"), pr.c.ref)
        .join(pr, pr.c.id == pw.c.probe_id)
        .outerjoin(pl, pl.c.id == pr.c.plugin_id)
        .where(pw.c.project_id == project_id, pw.c.week.in_(weeks))
        .order_by(pw.c.id)
    )
    return list((await conn.execute(query)).all())


async def active_probes(conn: AsyncConnection, project_id: int) -> list:
    """The project's active probes of a merged change or of a plugin's rule, by id."""
    pr, c, pl = tables.curator_probes, tables.curator_changes, tables.plugins
    query = (
        select(pr.c.id, pr.c.change_id, pr.c.label, pr.c.created_at, pl.c.name.label("plugin"), pr.c.ref)
        .outerjoin(c, c.c.id == pr.c.change_id)
        .outerjoin(pl, pl.c.id == pr.c.plugin_id)
        .where(
            pr.c.project_id == project_id,
            pr.c.state == "active",
            or_(c.c.state == "merged", pr.c.plugin_id.is_not(None)),
        )
        .order_by(pr.c.id)
    )
    return list((await conn.execute(query)).all())


async def unprobed(conn: AsyncConnection, project_id: int) -> list:
    """The project's merged changes with no active probe, the newest first, with their proposal's evidence and paths."""
    c, p, pr = tables.curator_changes, tables.proposals, tables.curator_probes
    has_active = exists().where(pr.c.change_id == c.c.id, pr.c.state == "active")
    query = (
        select(
            c.c.id,
            c.c.proposal_id,
            c.c.merged_at,
            p.c.title,
            p.c.run_id,
            p.c.evidence,
            p.c.finding_ids,
            p.c.paths,
            p.c.label,
        )
        .join_from(c, p, p.c.id == c.c.proposal_id)
        .where(c.c.project_id == project_id, c.c.state == "merged", ~has_active)
        .order_by(c.c.id.desc())
    )
    return list((await conn.execute(query)).all())


async def last_retired(conn: AsyncConnection, change_id: int):
    """(id, retired_note) of the change's newest retired probe, or None."""
    pr = tables.curator_probes
    return (
        await conn.execute(
            select(pr.c.id, pr.c.retired_note)
            .where(pr.c.change_id == change_id, pr.c.state == "retired")
            .order_by(pr.c.id.desc())
            .limit(1)
        )
    ).one_or_none()


async def fixes(conn: AsyncConnection, project_id: int, since: date) -> list:
    """The project's fix proposals still open, or made since ``since``, the newest first."""
    p = tables.proposals
    query = (
        select(p.c.id, p.c.fix_of, p.c.title, p.c.state, p.c.label)
        .where(
            p.c.project_id == project_id,
            p.c.fix_of.is_not(None),
            p.c.state.in_(OPEN) | (func.date(p.c.created_at) >= since),
        )
        .order_by(p.c.id.desc())
        .limit(MAX_UNPROBED)
    )
    return list((await conn.execute(query)).all())


async def replacements(conn: AsyncConnection, project_id: int, since: date) -> list:
    """The project's probes made since ``since`` in place of others, the newest first."""
    pr = tables.curator_probes
    query = (
        select(pr.c.id, pr.c.replaces, pr.c.change_id, pr.c.state, pr.c.created_at, pr.c.label)
        .where(
            pr.c.project_id == project_id,
            pr.c.replaces.is_not(None),
            func.date(pr.c.created_at) >= since,
        )
        .order_by(pr.c.id.desc())
        .limit(MAX_UNPROBED)
    )
    return list((await conn.execute(query)).all())


async def lock_schedule(conn: AsyncConnection, schedule_id: int) -> None:
    s = tables.schedules
    await conn.execute(select(s.c.id).where(s.c.id == schedule_id).with_for_update())


async def told(conn: AsyncConnection, project_id: int, week: date) -> bool:
    """Whether the project's notice of the report of the week of Monday ``week`` was told."""
    n = tables.notifications
    found = await conn.scalar(
        select(n.c.id)
        .where(
            n.c.project_id == project_id,
            n.c.notice_kind == runs.PROBE_REPORT_NOTICE,
            n.c.details["week"].astext == week.isoformat(),
        )
        .limit(1)
    )
    return found is not None
