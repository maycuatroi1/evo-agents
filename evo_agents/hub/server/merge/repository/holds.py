"""The queries of the hold of a merge check for the project's triage (``service.holds``): the owner of the plan run
that asked for the check, and the held check of a thread."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.merge.models.holds import KEY


async def owner(conn: AsyncConnection, row):
    """(the id and login of the owner of the plan run that asked for check ``row``, its project's name), or None."""
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.dispatched_by, u.c.login, p.c.name)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == row.run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def thread_check(conn: AsyncConnection, chat_id: int, root_id: int):
    """The merge check whose triage is the thread ``root_id`` of chat ``chat_id``, locked, or None."""
    m = tables.merge_checks
    query = (
        select(m)
        .where(
            m.c.verdict[KEY]["chat_id"].as_integer() == chat_id,
            m.c.verdict[KEY]["root_id"].as_integer() == root_id,
        )
        .with_for_update()
    )
    return (await conn.execute(query)).first()


async def dispatcher(conn: AsyncConnection, run_id: int) -> int | None:
    """Who dispatched run ``run_id``, or None."""
    r = tables.runs
    return (await conn.execute(select(r.c.dispatched_by).where(r.c.id == run_id))).scalar_one_or_none()
