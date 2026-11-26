"""What the job ``curator.changes`` reads again of the changes it no longer moves on itself
(``evo_agents.hub.server.changes.service.watch``): the changes left open with a pull request, the planned changes on
GitHub with their plan, and the member a GitHub login names."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def left_open(conn: AsyncConnection, every: timedelta, limit: int) -> list:
    """The changes left open for their owner with a pull request on GitHub, not read for ``every``, ``limit`` of
    them, with their repo's origin."""
    c, pr = tables.curator_changes, tables.project_repos
    query = (
        select(c, pr.c.origin)
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
        .where(
            c.c.state == "open",
            c.c.forge == "github",
            c.c.pr_number.is_not(None),
            c.c.updated_at < func.now() - every,
        )
        .order_by(c.c.updated_at)
        .limit(limit)
    )
    return list((await conn.execute(query)).all())


async def member_of(conn: AsyncConnection, login: str | None) -> int | None:
    if not login:
        return None
    u = tables.users
    return (await conn.execute(select(u.c.id).where(func.lower(u.c.login) == login.lower()))).scalar_one_or_none()


async def planned_on_github(conn: AsyncConnection, every: timedelta) -> list:
    """The planned changes on GitHub with a branch, not read for ``every``, with their repo's origin and their plan's
    body (``plan_body``)."""
    c, pr, pl = tables.curator_changes, tables.project_repos, tables.plans
    query = (
        select(c, pr.c.origin, pl.c.body.label("plan_body"))
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
        .join(pl, (pl.c.project_id == c.c.project_id) & (pl.c.plan_id == c.c.plan_id))
        .where(
            c.c.state == "planned",
            c.c.forge == "github",
            c.c.branch.is_not(None),
            c.c.updated_at < func.now() - every,
        )
        .order_by(c.c.updated_at)
    )
    return list((await conn.execute(query)).all())


async def touch_planned(conn: AsyncConnection, change_id: int) -> None:
    """Mark planned change ``change_id`` read now, so it is read again after the job's span."""
    c = tables.curator_changes
    await conn.execute(update(c).values(updated_at=func.now()).where(c.c.id == change_id, c.c.state == "planned"))
