"""The SQL of the flow after an archive's upload (``evo_agents.hub.server.kg_pipeline``): the upload, the builds and
kg_init runs it went through, the uploads nobody was told of, and the admins a notice goes to."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_kg import INIT_ACTIVE


def upload_columns():
    u = tables.kg_uploads
    return (
        u.c.id,
        u.c.project_id,
        u.c.state,
        u.c.label,
        u.c.error,
        u.c.build_id,
        u.c.report,
        u.c.uploaded_at,
        u.c.created_by,
        u.c.told_at,
    )


def _build_columns():
    b = tables.kg_builds
    return (b.c.id, b.c.status, b.c.error, b.c.nodes, b.c.edges, b.c.coverage)


def _init_columns():
    t = tables.kg_init_runs
    return (t.c.id, t.c.state, t.c.build_id, t.c.agent_run_id, t.c.body, t.c.error)


async def upload(conn: AsyncConnection, project_id: int, upload_id: int):
    """Upload ``upload_id`` of the project (``upload_columns``); None for none."""
    u = tables.kg_uploads
    return (
        await conn.execute(select(*upload_columns()).where(u.c.id == upload_id, u.c.project_id == project_id))
    ).first()


async def build(conn: AsyncConnection, project_id: int, build_id: int):
    """Build ``build_id`` of the project; None for none."""
    b = tables.kg_builds
    return (
        await conn.execute(select(*_build_columns()).where(b.c.id == build_id, b.c.project_id == project_id))
    ).first()


async def build_after(conn: AsyncConnection, project_id: int, build_id: int):
    """The next build of the project after build ``build_id``; None for none."""
    b = tables.kg_builds
    return (
        await conn.execute(
            select(*_build_columns()).where(b.c.project_id == project_id, b.c.id > build_id).order_by(b.c.id).limit(1)
        )
    ).first()


async def newest_init(conn: AsyncConnection, project_id: int, build_id: int):
    """The newest kg_init run of the project on build ``build_id`` or later; None for none."""
    t = tables.kg_init_runs
    return (
        await conn.execute(
            select(*_init_columns())
            .where(t.c.project_id == project_id, t.c.build_id >= build_id)
            .order_by(t.c.id.desc())
            .limit(1)
        )
    ).first()


async def active_init(conn: AsyncConnection, project_id: int):
    """A kg_init run of the project in flight; None for none."""
    t = tables.kg_init_runs
    return (
        await conn.execute(select(*_init_columns()).where(t.c.project_id == project_id, t.c.state.in_(INIT_ACTIVE)))
    ).first()


async def earlier_init_bodies(conn: AsyncConnection, project_id: int, build_id: int, before: int, limit: int) -> list:
    """The bodies of the ``limit`` newest kg_init runs of the project on build ``build_id`` or later before run
    ``before``, newest first."""
    t = tables.kg_init_runs
    rows = await conn.execute(
        select(t.c.body)
        .where(t.c.project_id == project_id, t.c.build_id >= build_id, t.c.id < before)
        .order_by(t.c.id.desc())
        .limit(limit)
    )
    return [body for (body,) in rows]


async def mark_told(conn: AsyncConnection, upload_id: int) -> bool:
    """Mark upload ``upload_id`` told; False when it was already."""
    u = tables.kg_uploads
    marked = await conn.execute(
        update(u).where(u.c.id == upload_id, u.c.told_at.is_(None)).values(told_at=func.now()).returning(u.c.id)
    )
    return marked.first() is not None


async def admins(conn: AsyncConnection, project_id: int) -> list[int]:
    """The users with the admin role on the project."""
    g = tables.grants
    return list(
        (await conn.execute(select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin"))).scalars()
    )


async def untold(conn: AsyncConnection, limit: int) -> list:
    """The first ``limit`` uploads past uploading that nobody was told of (``upload_columns`` and ``project``)."""
    u, p = tables.kg_uploads, tables.projects
    return list(
        (
            await conn.execute(
                select(*upload_columns(), p.c.name.label("project"))
                .join_from(u, p, p.c.id == u.c.project_id)
                .where(u.c.told_at.is_(None), u.c.state != "uploading")
                .order_by(u.c.id)
                .limit(limit)
            )
        ).all()
    )
