"""The rows the seed of a project's default system reads and writes (``service.seed``): the projects not seeded yet,
the Orchestrator's profile, and which system is the project's default."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def project_named(conn: AsyncConnection, name: str) -> bool:
    """Whether a project is named ``name``, which an agent's handle may not be."""
    p = tables.projects
    return await conn.scalar(select(p.c.id).where(p.c.name == name)) is not None


async def add_profile(conn: AsyncConnection, project_id: int, owner_id: int, values: dict) -> int | None:
    """Insert the profile ``values``; its id, or None when another agent holds its handle."""
    ap = tables.agent_profiles
    statement = (
        pg_insert(ap)
        .values(project_id=project_id, created_by=owner_id, **values)
        .on_conflict_do_nothing(index_elements=[ap.c.handle])
        .returning(ap.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def project_to_seed(conn: AsyncConnection, project_id: int):
    """The project's name, owner and when it was seeded, locked; None when another pass holds it or it is gone."""
    p = tables.projects
    query = select(p.c.name, p.c.created_by, p.c.systems_seeded_at).where(p.c.id == project_id)
    return (await conn.execute(query.with_for_update(skip_locked=True))).one_or_none()


async def mark_seeded(conn: AsyncConnection, project_id: int) -> None:
    p = tables.projects
    await conn.execute(update(p).values(systems_seeded_at=func.now()).where(p.c.id == project_id))


async def oldest_system(conn: AsyncConnection, project_id: int) -> int | None:
    s = tables.agent_systems
    return await conn.scalar(select(func.min(s.c.id)).where(s.c.project_id == project_id))


async def has_default(conn: AsyncConnection, project_id: int) -> bool:
    s = tables.agent_systems
    return await conn.scalar(select(s.c.id).where(s.c.project_id == project_id, s.c.is_default)) is not None


async def make_default(
    conn: AsyncConnection, project_id: int, *, system_id: int | None = None, slug: str | None = None
):
    """Mark the project's system ``system_id`` (or ``slug``) its default."""
    s = tables.agent_systems
    which = s.c.id == system_id if system_id is not None else s.c.slug == slug
    await conn.execute(update(s).values(is_default=True).where(s.c.project_id == project_id, which))


async def unseeded(conn: AsyncConnection) -> list[int]:
    """The projects not seeded yet, by id."""
    p = tables.projects
    return list((await conn.execute(select(p.c.id).where(p.c.systems_seeded_at.is_(None)).order_by(p.c.id))).scalars())
