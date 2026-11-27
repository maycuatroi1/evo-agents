"""The SQL of the archives a writer uploads as sources of a project's graph (``evo_agents.hub.server.kg_uploads`` and
``kg_upload_relay``): the rows of ``kg_uploads``, with the sender's login and the status of the build that followed."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def in_flight(conn: AsyncConnection, project_id: int, user_id: int, sha256: str, source: str):
    """The upload still ``uploading`` of the same archive (project, sender, SHA-256, source); None for none."""
    u = tables.kg_uploads
    return (
        await conn.execute(
            select(u).where(
                u.c.project_id == project_id,
                u.c.created_by == user_id,
                u.c.sha256 == sha256,
                u.c.source == source,
                u.c.state == "uploading",
            )
        )
    ).one_or_none()


async def insert(conn: AsyncConnection, row: dict) -> int:
    """A new upload of ``row``'s columns; its id."""
    u = tables.kg_uploads
    return (await conn.execute(u.insert().values(row).returning(u.c.id))).scalar_one()


async def find(conn: AsyncConnection, project_id: int, upload_id: int, *, lock: bool = False):
    """The upload ``upload_id`` of the project, locked with ``lock``; None for none."""
    u = tables.kg_uploads
    query = select(u).where(u.c.id == upload_id, u.c.project_id == project_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def touch(conn: AsyncConnection, upload_id: int, **values) -> None:
    """Set ``values`` on the upload, and its updated_at to now."""
    u = tables.kg_uploads
    await conn.execute(update(u).where(u.c.id == upload_id).values(values | {"updated_at": func.now()}))


async def joined(conn: AsyncConnection, upload_id: int, error: str | None) -> None:
    """The upload's parts are joined: ``extracting`` from now, or failed with ``error``."""
    if error is None:
        await touch(conn, upload_id, multipart_id=None, state="extracting", uploaded_at=func.now())
    else:
        await touch(conn, upload_id, multipart_id=None, state="failed", ended_at=func.now(), error=error)


async def listed(conn: AsyncConnection, project_id: int, *, upload_id: int | None, limit: int) -> list:
    """The project's uploads, newest first, each with ``login`` (its sender) and ``build_status``."""
    u, users, builds = tables.kg_uploads, tables.users, tables.kg_builds
    query = (
        select(u, users.c.login, builds.c.status.label("build_status"))
        .select_from(u.outerjoin(users, users.c.id == u.c.created_by).outerjoin(builds, builds.c.id == u.c.build_id))
        .where(u.c.project_id == project_id)
        .order_by(u.c.id.desc())
        .limit(limit)
    )
    if upload_id is not None:
        query = query.where(u.c.id == upload_id)
    return list((await conn.execute(query)).all())
