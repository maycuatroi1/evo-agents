"""The queries on curator_probe_runs: the pairs a week has yet to measure, the rows the night shift queues and an
admin requeues, a probe run's claim and its reports, and what the end of a probe run does to its row."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes, tables
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.probes.models.runs import ProbeRun


async def week_cost(conn: AsyncConnection, schedule_id: int, week: date) -> float:
    """What the probe runs schedule ``schedule_id`` queued for the nights of the ISO week of ``week`` cost."""
    r = tables.runs
    of_week = (
        r.c.schedule_id == schedule_id,
        r.c.kind == "probe",
        r.c.schedule_night.between(week, week + timedelta(days=6)),
    )
    found = await conn.execute(select(func.coalesce(func.sum(usage_cost(r.c.usage)), 0)).where(*of_week))
    return float(found.scalar_one() or 0)


async def measurable(conn: AsyncConnection, project_id: int) -> list:
    """The project's active probes of a merged change with a plan, or of a core plugin's rule, by id, each with its
    change's plan, repo and branch and its plugin's name."""
    pr, c, pl = tables.curator_probes, tables.curator_changes, tables.plugins
    found = await conn.execute(
        select(
            pr,
            c.c.plan_id.label("plan_id"),
            func.coalesce(c.c.repo, pr.c.repo).label("change_repo"),  # a plugin's probe: the files go in its repo
            c.c.branch.label("change_branch"),
            pl.c.name.label("plugin"),
        )
        .outerjoin(c, c.c.id == pr.c.change_id)
        .outerjoin(pl, pl.c.id == pr.c.plugin_id)
        .where(
            pr.c.project_id == project_id,
            pr.c.state == "active",
            or_(
                and_(c.c.state == "merged", c.c.plan_id.is_not(None)),
                and_(pr.c.source == probes.PLUGIN, pr.c.plugin_id.is_not(None)),
            ),
        )
        .order_by(pr.c.id)
    )
    return list(found.all())


async def rows_of(conn: AsyncConnection, probe_ids: list[int]) -> list:
    """Every row of curator_probe_runs of the probes ``probe_ids``."""
    pw = tables.curator_probe_runs
    return list((await conn.execute(select(pw).where(pw.c.probe_id.in_(probe_ids)))).all())


async def unmeasured_row(conn: AsyncConnection, row_id: int, why: str) -> None:
    """Row ``row_id``, when it is still queued, unmeasured for ``why``."""
    pw = tables.curator_probe_runs
    values = {"state": "unmeasured", "reason": why, "updated_at": func.now()}
    await conn.execute(update(pw).values(**values).where(pw.c.id == row_id, pw.c.state == "queued"))


async def insert_unless_held(conn: AsyncConnection, values: dict) -> None:
    """A new row with ``values``, unless the week holds one of its probe, runtime and model already."""
    await conn.execute(pg_insert(tables.curator_probe_runs).values(**values).on_conflict_do_nothing())


async def insert_row(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(pg_insert(tables.curator_probe_runs).values(**values))


async def update_row(conn: AsyncConnection, row_id: int, values: dict) -> None:
    """Write ``values`` on row ``row_id``, and its updated_at."""
    pw = tables.curator_probe_runs
    await conn.execute(update(pw).values(**values, updated_at=func.now()).where(pw.c.id == row_id))


async def week_rows(conn: AsyncConnection, probe_id: int, week: date) -> list:
    """(id, state) of the rows of probe ``probe_id`` of the week of Monday ``week``, by id, locked."""
    pw = tables.curator_probe_runs
    query = select(pw.c.id, pw.c.state).where(pw.c.probe_id == probe_id, pw.c.week == week)
    return list((await conn.execute(query.order_by(pw.c.id).with_for_update())).all())


async def requeue_rows(conn: AsyncConnection, row_ids: list[int]) -> None:
    """Rows ``row_ids`` back to queued, their attempts at 0, no reason and no key."""
    pw = tables.curator_probe_runs
    values = {"state": "queued", "attempts": 0, "reason": None, "probe_key": None, "updated_at": func.now()}
    await conn.execute(update(pw).values(**values).where(pw.c.id.in_(row_ids)))


async def run_view(conn: AsyncConnection, row_id: int) -> ProbeRun:
    """Row ``row_id`` as the API shows it."""
    pw = tables.curator_probe_runs
    row = (await conn.execute(select(pw).where(pw.c.id == row_id))).one()
    return ProbeRun(**{key: getattr(row, key) for key in ProbeRun.model_fields})


async def row_of_run(conn: AsyncConnection, run_id: int, *, running: bool = False):
    """The row of probe run ``run_id`` (only while it is running, with ``running``), locked; None without one."""
    pw = tables.curator_probe_runs
    query = select(pw).where(pw.c.run_id == run_id)
    if running:
        query = query.where(pw.c.state == "running")
    return (await conn.execute(query.with_for_update())).one_or_none()


async def probe_of(conn: AsyncConnection, probe_id: int):
    """The row of curator_probes of probe ``probe_id``, which exists."""
    pr = tables.curator_probes
    return (await conn.execute(select(pr).where(pr.c.id == probe_id))).one()


async def plugin_name(conn: AsyncConnection, plugin_id: int | None) -> str | None:
    """The name of plugin ``plugin_id``; None once it is removed."""
    pl = tables.plugins
    return (await conn.execute(select(pl.c.name).where(pl.c.id == plugin_id))).scalar_one_or_none()


async def task_of_run(conn: AsyncConnection, run_id: int):
    """(task, repo) of the probe probe run ``run_id`` measures; None without one."""
    pw, pr = tables.curator_probe_runs, tables.curator_probes
    return (
        await conn.execute(select(pr.c.task, pr.c.repo).join(pw, pw.c.probe_id == pr.c.id).where(pw.c.run_id == run_id))
    ).one_or_none()


async def held_run(conn: AsyncConnection, run_id: int):
    """(state, worker_id, kind) of run ``run_id``, locked; None without one."""
    r = tables.runs
    return (
        await conn.execute(select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update())
    ).one_or_none()
