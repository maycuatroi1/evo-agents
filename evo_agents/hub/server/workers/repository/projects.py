"""The queries of the projects a worker serves (``tables.worker_projects``): which they are, what still needs the
worker in one (its active runs, its night shift), how they change, and whether a member still writes to them."""

from __future__ import annotations

from sqlalchemy import BigInteger, any_, delete, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.access import ROLES
from evo_agents.hub.server.runs.service.kg_duties import kept_by
from evo_agents.hub.server.security import holds

NIGHT_SHIFT = "night_shift"  # the kind of schedule the charter of a project's Curator keeps on one worker
WRITER_ROLES = [role for role in ROLES if holds(role, "writer")]


async def served(conn: AsyncConnection, worker_id: int) -> dict[str, int]:
    """The projects the worker serves, name -> id, by name."""
    wp, projects = tables.worker_projects, tables.projects
    names = (
        select(projects.c.name, projects.c.id)
        .join_from(wp, projects, projects.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id)
        .order_by(projects.c.name)
    )
    return dict((await conn.execute(names)).all())


async def active_runs(conn: AsyncConnection, worker_id: int, project_ids: list[int]) -> list[tuple[int, int]]:
    """(project id, run id) of each run of ``project_ids`` in an active state held by or pinned to the worker, by
    id."""
    r = tables.runs
    active = (
        select(r.c.project_id, r.c.id)
        .where(
            r.c.project_id.in_(project_ids),
            r.c.state.in_(runs.ACTIVE_STATES),
            or_(r.c.worker_id == worker_id, r.c.pinned_worker_id == worker_id),
        )
        .order_by(r.c.id)
    )
    return [(project_id, run_id) for project_id, run_id in await conn.execute(active)]


async def night_shifts(conn: AsyncConnection, worker_id: int, project_ids: list[int]) -> list[int]:
    """The projects of ``project_ids`` whose night shift the worker keeps."""
    s = tables.schedules
    duty = select(s.c.project_id).where(s.c.project_id.in_(project_ids), s.c.kind == NIGHT_SHIFT, kept_by(s, worker_id))
    return list((await conn.execute(duty)).scalars())


async def drop(conn: AsyncConnection, worker_id: int, project_ids: list[int]) -> None:
    wp = tables.worker_projects
    await conn.execute(delete(wp).where(wp.c.worker_id == worker_id, wp.c.project_id.in_(project_ids)))


async def add(conn: AsyncConnection, worker_id: int, project_ids: list[int]) -> None:
    wp = tables.worker_projects
    await conn.execute(insert(wp), [{"worker_id": worker_id, "project_id": project_id} for project_id in project_ids])


async def writer_count(conn: AsyncConnection, owner_id: int, project_ids) -> int:
    """On how many of ``project_ids`` ``owner_id`` holds the writer role now."""
    grants = tables.grants
    owner_writes = (
        select(func.count())
        .select_from(grants)
        .where(
            grants.c.user_id == owner_id,
            grants.c.project_id == any_(literal(list(project_ids), ARRAY(BigInteger))),
            grants.c.role.in_(WRITER_ROLES),
        )
    )
    return (await conn.execute(owner_writes)).scalar_one()
