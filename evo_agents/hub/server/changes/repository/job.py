"""What the job ``curator.changes`` (``evo_agents.hub.server.changes.service.job``) reads and writes of the changes it
moves on: the changes due, the owner of a project's night shift and whether it is paused, a change's state under its
row lock, its reason, and its proposal's title."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

NIGHT_SHIFT = "night_shift"


async def due_changes(conn: AsyncConnection) -> list:
    """The changes waiting for their pull request or judged, with their repo's origin and whether each works in
    several repos (``several``), oldest first."""
    c, pr, cr = tables.curator_changes, tables.project_repos, tables.curator_change_repos
    several = exists().where(cr.c.change_id == c.c.id).label("several")  # a change in several repos (service.repos)
    query = (
        select(c, pr.c.origin, several)
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
        .where(c.c.state.in_(("pr_pending", "judged")))
        .order_by(c.c.id)
    )
    return list((await conn.execute(query)).all())


async def owner_of(conn: AsyncConnection, project_id: int) -> int | None:
    """The owner of the project's night shift, or None."""
    s = tables.schedules
    query = select(s.c.owner_id).where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
    return (await conn.execute(query)).scalar_one_or_none()


async def title_and_project(conn: AsyncConnection, proposal_id: int) -> tuple[str, str]:
    """(the title of proposal ``proposal_id``, the name of its project)."""
    pr, p = tables.proposals, tables.projects
    query = select(pr.c.title, p.c.name).join_from(pr, p, p.c.id == pr.c.project_id).where(pr.c.id == proposal_id)
    title, project = (await conn.execute(query)).one()
    return title, project


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    return (await conn.execute(select(tables.projects.c.name).where(tables.projects.c.id == project_id))).scalar_one()


async def night_paused(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project's night shift is paused (a member paused it, or its circuit breaker): it merges nothing."""
    s = tables.schedules
    query = select(exists().where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT, s.c.paused_at.is_not(None)))
    return bool((await conn.execute(query)).scalar_one())


async def unchanged(conn: AsyncConnection, change) -> bool:
    """Whether ``change`` is still as this pass read it, in the same state at the same head, its row locked until the
    caller's transaction ends: a person may have closed it, or an agent merged it, since."""
    c = tables.curator_changes
    query = select(c.c.state, c.c.head_sha).where(c.c.id == change.id).with_for_update()
    row = (await conn.execute(query)).first()
    return row is not None and (row.state, row.head_sha) == (change.state, change.head_sha)


async def set_reason(conn: AsyncConnection, change_id: int, why: str | None) -> None:
    """Set the reason of change ``change_id`` alone, leaving its state and when it last moved as they are."""
    c = tables.curator_changes
    await conn.execute(update(c).values(reason=why).where(c.c.id == change_id))


async def locked_state(conn: AsyncConnection, change_id: int) -> str | None:
    """The state of change ``change_id``, its row locked until the caller's transaction ends; None without it."""
    c = tables.curator_changes
    return (await conn.execute(select(c.c.state).where(c.c.id == change_id).with_for_update())).scalar_one_or_none()


async def older_than(conn: AsyncConnection, moment: datetime, span: timedelta) -> bool:
    return bool((await conn.execute(select(func.now() - span > moment))).scalar_one())
