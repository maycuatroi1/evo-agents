"""The SQL of the agent profiles of a project: a profile by handle, the profiles of a project, the versions of one,
the insert, the next version and the soft delete of a profile, and the blob row of its avatar."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select


async def profile_row(conn: AsyncConnection, project_id: int, handle: str, *, lock: bool = False) -> Profile | None:
    """The profile ``handle`` of the project, its row locked when ``lock``; None when the project has none."""
    ap = tables.agent_profiles
    query = profile_select().where(ap.c.project_id == project_id, ap.c.handle == handle)
    row = (await conn.execute(query.with_for_update(of=ap) if lock else query)).one_or_none()
    return None if row is None else profile_of(row)


async def project_profiles(conn: AsyncConnection, project_id: int, deleted: bool) -> list[Profile]:
    """The profiles of the project (the deleted ones too when ``deleted``), the default first, then by handle."""
    ap = tables.agent_profiles
    query = profile_select().where(ap.c.project_id == project_id)
    if not deleted:
        query = query.where(ap.c.deleted_at.is_(None))
    rows = await conn.execute(query.order_by(ap.c.is_default.desc(), ap.c.handle))
    return [profile_of(row) for row in rows]


async def versions(conn: AsyncConnection, profile_id: int) -> list:
    """(version, body, changed_by, changed_at) of every version of the profile, the latest first."""
    v, u = tables.agent_profile_versions, tables.users
    query = (
        select(v.c.version, v.c.body, u.c.login.label("changed_by"), v.c.changed_at)
        .join_from(v, u, u.c.id == v.c.changed_by)
        .where(v.c.profile_id == profile_id)
        .order_by(v.c.version.desc())
    )
    return list(await conn.execute(query))


async def project_named(conn: AsyncConnection, name: str) -> bool:
    """Whether a project is named ``name``."""
    p = tables.projects
    return (await conn.execute(select(p.c.id).where(p.c.name == name))).first() is not None


async def insert_profile(conn: AsyncConnection, project_id: int, user_id: int, values: dict) -> int | None:
    """The id of the profile ``values`` made in the project; None when its handle is taken."""
    ap = tables.agent_profiles
    statement = (
        pg_insert(ap)
        .values(project_id=project_id, created_by=user_id, **values)
        .on_conflict_do_nothing(index_elements=[ap.c.handle])
        .returning(ap.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def write_profile(conn: AsyncConnection, profile_id: int, changes: dict, version: int) -> None:
    """Write ``changes`` to the profile at version ``version``, as updated now."""
    ap = tables.agent_profiles
    await conn.execute(
        update(ap).values(**changes, version=version, updated_at=func.now()).where(ap.c.id == profile_id)
    )


async def mark_deleted(conn: AsyncConnection, profile_id: int) -> None:
    """Soft-delete the profile: it is marked deleted now."""
    ap = tables.agent_profiles
    await conn.execute(update(ap).values(deleted_at=func.now()).where(ap.c.id == profile_id))


async def hold_blob(conn: AsyncConnection, project_id: int, sha256: str, size: int, kind: str, user_id: int) -> None:
    """Record that the project holds blob ``sha256`` of kind ``kind``, unless it does already."""
    b = tables.blobs
    await conn.execute(
        pg_insert(b)
        .values(project_id=project_id, sha256=sha256, size=size, kind=kind, created_by=user_id)
        .on_conflict_do_nothing()
    )
