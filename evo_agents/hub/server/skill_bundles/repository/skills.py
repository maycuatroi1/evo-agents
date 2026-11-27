"""The SQL of skills: the skills and their versions, and what reading and publishing them reads of grants, the blobs
a holder committed and the workers of the pools."""

from __future__ import annotations

from sqlalchemy import Select, and_, func, insert, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.skill_bundles.models.skills import Place


async def granted_project(conn: AsyncConnection, user_id: int, project: str) -> int | None:
    """The id of project ``project`` when ``user_id`` holds a grant on it; None otherwise, or when there is none."""
    projects, grants = tables.projects, tables.grants
    granted = (
        select(projects.c.id)
        .join_from(projects, grants, and_(grants.c.project_id == projects.c.id, grants.c.user_id == user_id))
        .where(projects.c.name == project)
    )
    row = (await conn.execute(granted)).first()
    return None if row is None else row.id


def latest_skills(with_global: tuple[str, ...], project_ids: list[int]) -> Select:
    """The skills of Skill, column by field name, each with its latest version: the global ones whose audience is in
    ``with_global``, and those of the projects ``project_ids``."""
    skills, projects, users, versions = tables.skills, tables.projects, tables.users, tables.skill_versions
    latest = (
        select(versions)
        .where(versions.c.skill_id == skills.c.id)
        .order_by(versions.c.version.desc())
        .limit(1)
        .lateral("v")
    )
    wanted = [and_(skills.c.scope == "project", skills.c.project_id.in_(project_ids))]
    if with_global:
        wanted.insert(0, and_(skills.c.scope == "global", skills.c.audience.in_(with_global)))
    return (
        select(
            skills.c.scope,
            projects.c.name.label("project"),
            skills.c.name,
            latest.c.version,
            latest.c.description,
            latest.c.sha256,
            latest.c.size,
            latest.c.source_repo,
            latest.c.source_commit,
            users.c.login.label("published_by"),
            latest.c.published_at,
            skills.c.audience,
        )
        .select_from(
            skills.outerjoin(projects, projects.c.id == skills.c.project_id)
            .join(latest, true())
            .join(users, users.c.id == latest.c.published_by)
        )
        .where(or_(*wanted))
        .order_by(skills.c.scope, projects.c.name.nulls_first(), func.lower(skills.c.name))
    )


async def latest_rows(conn: AsyncConnection, page: Paging, with_global: tuple[str, ...], project_ids: list[int]):
    """The rows of ``latest_skills``, cut to ``page``."""
    return await page.rows(conn, latest_skills(with_global, project_ids))


async def granted_ids(conn: AsyncConnection, user: Principal) -> list[int]:
    """The projects whose skills a caller lists: those it holds a grant on, the run's alone for the agent of a run."""
    grants, projects = tables.grants, tables.projects
    query = (
        select(grants.c.project_id)
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .where(grants.c.user_id == user.user_id)
    )
    if user.run_project is not None:
        query = query.where(projects.c.name == user.run_project)
    return list((await conn.execute(query)).scalars())


def _versions(skill_id: int) -> Select:
    """The versions of skill ``skill_id`` with what Version shows, column by field name, the latest first."""
    versions, users = tables.skill_versions, tables.users
    return (
        select(
            versions.c.version,
            versions.c.description,
            versions.c.sha256,
            versions.c.size,
            versions.c.source_repo,
            versions.c.source_commit,
            users.c.login.label("published_by"),
            versions.c.published_at,
        )
        .join_from(versions, users, users.c.id == versions.c.published_by)
        .where(versions.c.skill_id == skill_id)
        .order_by(versions.c.version.desc())
    )


async def versions(conn: AsyncConnection, skill_id: int):
    """The rows of ``_versions``: every version of skill ``skill_id``, the latest first."""
    return (await conn.execute(_versions(skill_id))).all()


async def latest_version(conn: AsyncConnection, skill_id: int):
    """The row of ``_versions`` of the latest version of skill ``skill_id``."""
    return (await conn.execute(_versions(skill_id).limit(1))).one()


async def find(conn: AsyncConnection, place: Place, name: str, *, lock: bool = False):
    """(id, name, created_at, audience) of skill ``name`` in ``place``, matched ignoring case; None when there is none
    or it is for an audience the caller is not in."""
    skills = tables.skills
    query = select(skills.c.id, skills.c.name, skills.c.created_at, skills.c.audience).where(
        skills.c.scope == place.scope,
        skills.c.project_id.is_not_distinct_from(place.project_id),
        func.lower(skills.c.name) == func.lower(name),
        skills.c.audience.in_(place.audiences),
    )
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).first()


async def latest_bundle(conn: AsyncConnection, skill_id: int):
    """The number and SHA-256 of the latest version of skill ``skill_id``; None when it has none."""
    versions = tables.skill_versions
    query = select(versions.c.version, versions.c.sha256).where(versions.c.skill_id == skill_id)
    return (await conn.execute(query.order_by(versions.c.version.desc()).limit(1))).first()


async def committed(conn: AsyncConnection, place: Place, sha256: str):
    """The row (size,) of blob ``sha256`` as the holder of ``place`` committed it; None when it did not."""
    blobs = tables.blobs
    held = select(blobs.c.size).where(
        blobs.c.project_id.is_not_distinct_from(place.project_id), blobs.c.sha256 == sha256
    )
    return (await conn.execute(held)).first()


async def set_audience(conn: AsyncConnection, skill_id: int, audience: str) -> None:
    skills = tables.skills
    await conn.execute(update(skills).where(skills.c.id == skill_id).values(audience=audience))


async def add_skill(conn: AsyncConnection, place: Place, name: str, created_by: int, audience: str) -> None:
    """Skill ``name`` in ``place``, unless one of that name is there already."""
    skills = tables.skills
    new_skill = pg_insert(skills).values(
        scope=place.scope, project_id=place.project_id, name=name, created_by=created_by, audience=audience
    )
    await conn.execute(new_skill.on_conflict_do_nothing().returning(skills.c.id))


async def add_version(conn: AsyncConnection, **values) -> None:
    """A row of skill_versions with ``values``, column by name."""
    await conn.execute(insert(tables.skill_versions).values(**values))


async def bundle_of(conn: AsyncConnection, skill_id: int, version: int | None):
    """The number, SHA-256 and size of version ``version`` of skill ``skill_id``, the latest without one; None when
    there is none."""
    versions = tables.skill_versions
    query = select(versions.c.version, versions.c.sha256, versions.c.size).where(versions.c.skill_id == skill_id)
    if version is not None:
        query = query.where(versions.c.version == version)
    return (await conn.execute(query.order_by(versions.c.version.desc()).limit(1))).first()


async def live_pool(conn: AsyncConnection, token_id) -> int | None:
    """The pool of the live worker whose token is ``token_id``; None for a worker of no pool, or none."""
    w = tables.workers
    query = select(w.c.pool_id).where(w.c.token_id == token_id, w.c.revoked_at.is_(None))
    return (await conn.execute(query)).scalar_one_or_none()
