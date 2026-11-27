"""The SQL of a project's retrieval question sets (``evo_agents.hub.server.kg_eval_sets``, and the starter questions of
``evo_agents.hub.server.kg.service.questions``): the latest version, the project's lock, a new version."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def latest_row(conn: AsyncConnection, project_id: int, version: int | None = None):
    """(name, version, digest, questions, created_at) of the latest version of the project's set, or of ``version``;
    None without one."""
    s = tables.kg_eval_sets
    query = select(s.c.name, s.c.version, s.c.digest, s.c.questions, s.c.created_at).where(s.c.project_id == project_id)
    if version is not None:
        query = query.where(s.c.version == version)
    return (await conn.execute(query.order_by(s.c.version.desc()).limit(1))).one_or_none()


async def lock_project(conn: AsyncConnection, project_id: int) -> None:
    """Lock the project's row, so that two puts of its set number their versions one after the other."""
    await conn.execute(select(tables.projects.c.id).where(tables.projects.c.id == project_id).with_for_update())


async def add_set(
    conn: AsyncConnection,
    project_id: int,
    version: int,
    name: str,
    entries: list[dict],
    digest: str,
    created_by: int | None,
) -> None:
    s = tables.kg_eval_sets
    await conn.execute(
        s.insert().values(
            project_id=project_id,
            version=version,
            name=name,
            questions=entries,
            digest=digest,
            created_by=created_by,
        )
    )
