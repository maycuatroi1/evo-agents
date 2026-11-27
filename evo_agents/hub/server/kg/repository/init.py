"""The SQL of a project's kg_init runs (``evo_agents.hub.server.kg_init`` says what they do): the rows of kg_init_runs
with their project, the latest build, who dispatches the review run, the newest optimize run and charter, and the
state of the review run. ``evo_agents.hub.kg_init_db`` holds the rows the builds and the job share."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_kg import INIT_ACTIVE


async def now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(select(func.now()))).scalar_one()


async def set_run(conn: AsyncConnection, run_id: int, **values) -> None:
    t = tables.kg_init_runs
    await conn.execute(update(t).where(t.c.id == run_id).values(**values, updated_at=func.now()))


def context():
    """The runs with their project's name."""
    t, p = tables.kg_init_runs, tables.projects
    return select(t, p.c.name.label("project")).join_from(t, p, p.c.id == t.c.project_id)


async def active_contexts(conn: AsyncConnection) -> Sequence[Any]:
    """The runs in flight with their project's name, oldest first."""
    t = tables.kg_init_runs
    return (await conn.execute(context().where(t.c.state.in_(INIT_ACTIVE)).order_by(t.c.id))).all()


async def latest_build(conn: AsyncConnection, project_id: int):
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded")
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def asker_of(conn: AsyncConnection, ctx) -> tuple[int, str] | None:
    """(id, login) of the user the review run is dispatched by: who asked, else the first admin of the project."""
    u, g = tables.users, tables.grants
    if ctx.requested_by is not None:
        row = (await conn.execute(select(u.c.id, u.c.login).where(u.c.id == ctx.requested_by))).first()
        if row is not None:
            return row.id, row.login
    query = (
        select(u.c.id, u.c.login)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == ctx.project_id, g.c.role == "admin")
        .order_by(u.c.id)
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else (row.id, row.login)


async def last_optimize(conn: AsyncConnection, project_id: int):
    """The newest KG optimize run that ended done or reverted: id, state, body, asked_weight; None without one."""
    o = tables.kg_optimize_runs
    query = (
        select(o.c.id, o.c.state, o.c.body, o.c.asked_weight)
        .where(o.c.project_id == project_id, o.c.ended_at.is_not(None), o.c.state.in_(("done", "reverted")))
        .order_by(o.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def charter_body(conn: AsyncConnection, project_id: int) -> dict | None:
    """The body of the project's newest charter revision, None without one."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def run_state(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.state).where(r.c.id == run_id))).scalar_one_or_none()


async def run_for_update(conn: AsyncConnection, project_id: int, run_id: int):
    """Run ``run_id`` of the project, locked for the transaction; None when the project has no such run."""
    t = tables.kg_init_runs
    query = select(t).where(t.c.id == run_id, t.c.project_id == project_id).with_for_update()
    return (await conn.execute(query)).first()


async def has_config(conn: AsyncConnection, project_id: int) -> bool:
    c = tables.kg_configs
    return (await conn.execute(select(c.c.project_id).where(c.c.project_id == project_id))).first() is not None


async def project_names(conn: AsyncConnection) -> list[str]:
    p = tables.projects
    return list((await conn.execute(select(p.c.name).order_by(p.c.name))).scalars().all())


async def runs_of(conn: AsyncConnection, project_id: int, limit: int) -> Sequence[Any]:
    """The project's newest ``limit`` runs, newest first."""
    t = tables.kg_init_runs
    query = select(t).where(t.c.project_id == project_id).order_by(t.c.id.desc()).limit(limit)
    return (await conn.execute(query)).all()
