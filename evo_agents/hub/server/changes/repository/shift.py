"""What the night shift's side of the Curator's changes (``evo_agents.hub.server.changes.service.shift``) reads and
writes: the owner's git secret, the planned changes and the change waiting for its Judge, the judge runs of a plan
still active, and the reason a planned change waits with."""

from __future__ import annotations

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def owner_secret(conn: AsyncConnection, owner_id: int, project_id: int, name: str) -> bool:
    """Whether the owner has a live git secret ``name`` bound to the project."""
    s, b = tables.secrets, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    query = select(
        exists().where(
            s.c.owner_id == owner_id,
            s.c.name == name,
            s.c.kind == "git",
            s.c.deleted_at.is_(None),
            (s.c.expires_at.is_(None)) | (s.c.expires_at > func.now()),
            bound,
        )
    )
    return bool((await conn.execute(query)).scalar_one())


async def set_planned_reason(conn: AsyncConnection, change_id: int, text: str | None) -> None:
    """Set ``text`` as the reason of change ``change_id`` while it is planned and its reason says something else."""
    c = tables.curator_changes
    await conn.execute(
        update(c)
        .values(reason=text, updated_at=func.now())
        .where(c.c.id == change_id, c.c.state == "planned", c.c.reason.is_distinct_from(text))
    )


async def judging(conn: AsyncConnection, project_id: int, plan_id: str) -> bool:
    """Whether a judge run of plan ``plan_id`` is still active."""
    r = tables.runs
    query = select(
        exists().where(
            r.c.project_id == project_id,
            r.c.plan_id == plan_id,
            r.c.kind == "judge",
            r.c.state.in_(runs.ACTIVE_STATES),
        )
    )
    return bool((await conn.execute(query)).scalar_one())


async def planned(conn: AsyncConnection, project_id: int, only: int | None) -> list:
    """The planned changes of the project, oldest first, or change ``only`` alone when it is planned."""
    c = tables.curator_changes
    query = select(c).where(c.c.project_id == project_id, c.c.state == "planned")
    return list((await conn.execute((query.where(c.c.id == only) if only else query).order_by(c.c.id))).all())


async def judge_waiting(conn: AsyncConnection, project_id: int):
    """The oldest change of the project that waits for its Judge, with its proposal's label and title, or None."""
    c, p = tables.curator_changes, tables.proposals
    query = (
        select(c, p.c.label.label("proposal_label"), p.c.title.label("proposal_title"))
        .join_from(c, p, p.c.id == c.c.proposal_id)
        .where(c.c.project_id == project_id, c.c.state == "judge_pending")
        .order_by(c.c.id)
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()
