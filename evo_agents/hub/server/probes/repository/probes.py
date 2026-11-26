"""The queries on curator_probes, and on the changes and proposals a probe is added to."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def query():
    """The columns of a probe's view, its label and the login of who made it."""
    pr, u, pl = tables.curator_probes, tables.users, tables.plugins
    return select(
        pr.c.id,
        pr.c.project_id,
        pr.c.proposal_id,
        pr.c.change_id,
        pr.c.repo,
        pr.c.base_sha.label("commit"),
        pr.c.task,
        pr.c.verify,
        pr.c.state,
        pr.c.source,
        u.c.login.label("created_by"),
        pr.c.created_run_id,
        pr.c.retired_at,
        pr.c.retired_note,
        pr.c.created_at,
        pr.c.updated_at,
        pr.c.replaces,
        pl.c.name.label("plugin"),
        pr.c.ref,
        pr.c.label,
    ).select_from(pr.outerjoin(u, u.c.id == pr.c.created_by).outerjoin(pl, pl.c.id == pr.c.plugin_id))


async def insert_probe(conn: AsyncConnection, values: dict) -> int:
    """A new row of curator_probes with ``values``; its id."""
    pr = tables.curator_probes
    return (await conn.execute(insert(pr).values(**values).returning(pr.c.id))).scalar_one()


async def attach(conn: AsyncConnection, proposal_id: int, change_id: int) -> None:
    """Give the probes of proposal ``proposal_id`` that have no change yet the change ``change_id``."""
    pr = tables.curator_probes
    await conn.execute(
        update(pr).values(change_id=change_id).where(pr.c.proposal_id == proposal_id, pr.c.change_id.is_(None))
    )


async def newest_active_of_proposal(conn: AsyncConnection, proposal_id: int):
    """The view's row of the newest active probe of proposal ``proposal_id``, or None."""
    pr = tables.curator_probes
    return (
        await conn.execute(
            query().where(pr.c.proposal_id == proposal_id, pr.c.state == "active").order_by(pr.c.id.desc()).limit(1)
        )
    ).one_or_none()


async def active_on_change(conn: AsyncConnection, change_id: int) -> int | None:
    """The id of the active probe of change ``change_id``, or None."""
    pr = tables.curator_probes
    return (
        await conn.execute(select(pr.c.id).where(pr.c.change_id == change_id, pr.c.state == "active").limit(1))
    ).scalar_one_or_none()


async def active_on_proposal(conn: AsyncConnection, proposal_id: int) -> int | None:
    """The id of the active probe of proposal ``proposal_id``, or None."""
    pr = tables.curator_probes
    return (
        await conn.execute(select(pr.c.id).where(pr.c.proposal_id == proposal_id, pr.c.state == "active").limit(1))
    ).scalar_one_or_none()


async def listed(
    conn: AsyncConnection, project_id: int, *, change: int | None, proposal: int | None, state: str | None
) -> list:
    """The view's rows of the project's probes, newest first, of ``change``, ``proposal`` and ``state`` when given."""
    pr = tables.curator_probes
    query_ = query().where(pr.c.project_id == project_id)
    if change is not None:
        query_ = query_.where(pr.c.change_id == change)
    if proposal is not None:
        query_ = query_.where(pr.c.proposal_id == proposal)
    if state is not None:
        query_ = query_.where(pr.c.state == state)
    return list((await conn.execute(query_.order_by(pr.c.id.desc()))).all())


async def of_project(conn: AsyncConnection, project_id: int, probe_id: int, *, lock: bool = False):
    """The view's row of probe ``probe_id`` of the project, locked with ``lock``; None without one."""
    pr = tables.curator_probes
    query_ = query().where(pr.c.project_id == project_id, pr.c.id == probe_id)
    if lock:
        query_ = query_.with_for_update(of=pr)
    return (await conn.execute(query_)).one_or_none()


async def by_id(conn: AsyncConnection, probe_id: int):
    """The view's row of probe ``probe_id``, which exists."""
    pr = tables.curator_probes
    return (await conn.execute(query().where(pr.c.id == probe_id))).one()


async def revise(conn: AsyncConnection, probe_id: int, values: dict, *, user_id: int | None) -> None:
    """Write ``values`` on probe ``probe_id``, as ``user_id``."""
    pr = tables.curator_probes
    await conn.execute(
        update(pr).values(**values).values(updated_by=user_id, updated_at=func.now()).where(pr.c.id == probe_id)
    )


async def change_target(conn: AsyncConnection, project_id: int, change_id: int):
    """The change ``change_id`` of the project (id, state, repo, proposal_id) with its proposal's label, or None."""
    c, p = tables.curator_changes, tables.proposals
    return (
        await conn.execute(
            select(c.c.id, c.c.state, c.c.repo, c.c.proposal_id, p.c.label)
            .join_from(c, p, p.c.id == c.c.proposal_id)
            .where(c.c.project_id == project_id, c.c.id == change_id)
        )
    ).one_or_none()


async def proposal_target(conn: AsyncConnection, project_id: int, proposal_id: int | None):
    """The proposal ``proposal_id`` of the project (id, label), or None."""
    p = tables.proposals
    return (
        await conn.execute(select(p.c.id, p.c.label).where(p.c.project_id == project_id, p.c.id == proposal_id))
    ).one_or_none()


async def change_of_proposal(conn: AsyncConnection, proposal_id: int) -> int | None:
    """The change accepting proposal ``proposal_id`` made, or None."""
    c = tables.curator_changes
    return (await conn.execute(select(c.c.id).where(c.c.proposal_id == proposal_id))).scalar_one_or_none()
