"""The SQL of the projects (``evo_agents.hub.server.projects``): a caller's access to one project in one round trip,
the projects a caller sees with their sinks and repos, and registering a project with its sinks and repos."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Row, ScalarSelect, and_, bindparam, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.paging import Page
from evo_agents.hub.server.project_registry.models.projects import clearance_fields


def with_grant(user_id):
    """Projects left joined to the grant ``user_id`` holds on each, if any."""
    projects, grants = tables.projects, tables.grants
    return projects.outerjoin(grants, and_(grants.c.project_id == projects.c.id, grants.c.user_id == user_id))


def sinks_of(project_id) -> ScalarSelect:
    """The sinks of project ``project_id`` as one JSONB array of {id, kind, clearance}, by id; [] without one."""
    ps = tables.project_sinks
    sink = func.jsonb_build_object("id", ps.c.sink_id, "kind", ps.c.kind, "clearance", ps.c.clearance)
    listed = func.jsonb_agg(aggregate_order_by(sink, ps.c.sink_id), type_=JSONB)
    return (
        select(func.coalesce(listed, literal([], JSONB), type_=JSONB)).where(ps.c.project_id == project_id)
    ).scalar_subquery()


# Built once, as every request that names a project runs it (docs/hub.md, Data access): project :name, the grant
# user :user_id holds on it, and its sinks, in one round trip.
_ACCESS = (
    select(
        tables.projects.c.id,
        tables.projects.c.levels,
        tables.projects.c.locations,
        tables.projects.c.default_label,
        tables.grants.c.role,
        tables.grants.c.max_level,
        sinks_of(tables.projects.c.id).label("sinks"),
    )
    .select_from(with_grant(bindparam("user_id")))
    .where(tables.projects.c.name == bindparam("name"))
)


async def access_row(conn: AsyncConnection, user_id: int | None, name: str):
    """(id, levels, locations, default_label, role, max_level, sinks) of project ``name`` with the grant user
    ``user_id`` holds on it, or None when it is not registered."""
    return (await conn.execute(_ACCESS, {"user_id": user_id, "name": name})).first()


async def held(conn: AsyncConnection, project_id: int) -> dict:
    """What the hub holds for project ``project_id``, in the form ``service.projects.desired`` gives it."""
    projects, project_sinks, project_repos = tables.projects, tables.project_sinks, tables.project_repos
    project = (
        await conn.execute(
            select(
                projects.c.levels,
                projects.c.locations,
                projects.c.default_label,
                projects.c.cluster,
                projects.c.workspace,
                projects.c.harness_path,
            ).where(projects.c.id == project_id)
        )
    ).one()
    sinks = await conn.execute(
        select(project_sinks.c.sink_id, project_sinks.c.kind, project_sinks.c.clearance).where(
            project_sinks.c.project_id == project_id
        )
    )
    repos = await conn.execute(
        select(
            project_repos.c.name, project_repos.c.origin, project_repos.c.default_branch, project_repos.c.path
        ).where(project_repos.c.project_id == project_id)
    )
    return {
        "levels": project.levels,
        "locations": project.locations,
        "default_label": project.default_label,
        "harness": (project.cluster, project.workspace, project.harness_path),
        "sinks": sorted((sink.sink_id, sink.kind, clearance_fields(sink.clearance)) for sink in sinks),
        "repos": sorted(tuple(repo) for repo in repos),
    }


async def write_children(conn: AsyncConnection, project_id: int, desired: dict) -> None:
    """Replace the sinks and repos of project ``project_id`` with the ones of ``desired``."""
    project_sinks, project_repos = tables.project_sinks, tables.project_repos
    await conn.execute(delete(project_sinks).where(project_sinks.c.project_id == project_id))
    await conn.execute(delete(project_repos).where(project_repos.c.project_id == project_id))
    sinks = [
        {"project_id": project_id, "sink_id": sink, "kind": kind, "clearance": clearance}
        for sink, kind, clearance in desired["sinks"]
    ]
    if sinks:
        await conn.execute(insert(project_sinks), sinks)
    repos = [
        {"project_id": project_id, "name": name, "origin": origin, "default_branch": branch, "path": path}
        for name, origin, branch, path in desired["repos"]
    ]
    if repos:
        await conn.execute(insert(project_repos), repos)


async def stranded_grants(conn: AsyncConnection, project_id: int, levels: list[str]) -> Sequence[Row]:
    """(login, max_level) of the grants on project ``project_id`` that reach a level not in ``levels``, by login."""
    grants, users = tables.grants, tables.users
    found = await conn.execute(
        select(users.c.login, grants.c.max_level)
        .join_from(grants, users, users.c.id == grants.c.user_id)
        .where(grants.c.project_id == project_id, grants.c.max_level.not_in(levels))
        .order_by(func.lower(users.c.login))
    )
    return found.all()


def visible(user_id: int, admin: bool, run_project: str | None, name: str | None, archived: bool):
    """The projects user ``user_id`` holds a grant on (every one for a hub ``admin``), only ``name`` when given, and
    only ``run_project`` for the agent of a run, the archived ones only with ``archived``; with the user's grant, by
    name."""
    projects, grants, archiver = tables.projects, tables.grants, tables.users.alias("archiver")
    query = select(
        projects.c.id,
        projects.c.name,
        projects.c.cluster,
        projects.c.workspace,
        projects.c.harness_path,
        projects.c.levels,
        projects.c.locations,
        projects.c.default_label,
        projects.c.created_at,
        projects.c.updated_at,
        projects.c.archived_at,
        archiver.c.login.label("archived_by"),
        grants.c.role,
        grants.c.max_level,
    ).select_from(with_grant(user_id).outerjoin(archiver, archiver.c.id == projects.c.archived_by))
    if not archived:
        query = query.where(projects.c.archived_at.is_(None))
    if not admin:
        query = query.where(grants.c.user_id.is_not(None))
    if name is not None:
        query = query.where(projects.c.name == name)
    if run_project is not None:
        query = query.where(projects.c.name == run_project)
    return query.order_by(projects.c.name)


async def visible_rows(
    conn: AsyncConnection,
    user_id: int,
    admin: bool,
    run_project: str | None,
    name: str | None,
    *,
    page: Page,
    archived: bool,
):
    """The rows of ``visible`` on ``page``."""
    return await page.rows(conn, visible(user_id, admin, run_project, name, archived))


async def sinks_of_projects(conn: AsyncConnection, ids: list[int]):
    """(project_id, sink_id, kind, clearance) of the sinks of the projects ``ids``, by sink id."""
    project_sinks = tables.project_sinks
    return await conn.execute(
        select(project_sinks.c.project_id, project_sinks.c.sink_id, project_sinks.c.kind, project_sinks.c.clearance)
        .where(project_sinks.c.project_id.in_(ids))
        .order_by(project_sinks.c.sink_id)
    )


async def repos_of_projects(conn: AsyncConnection, ids: list[int]):
    """(project_id, name, origin, default_branch, path) of the repos of the projects ``ids``, by name."""
    project_repos = tables.project_repos
    return await conn.execute(
        select(
            project_repos.c.project_id,
            project_repos.c.name,
            project_repos.c.origin,
            project_repos.c.default_branch,
            project_repos.c.path,
        )
        .where(project_repos.c.project_id.in_(ids))
        .order_by(project_repos.c.name)
    )


async def locked_id(conn: AsyncConnection, name: str, *, one: bool = False) -> int | None:
    """The id of project ``name``, its row locked until the transaction ends; None when it is not registered, or
    NoResultFound with ``one``."""
    projects = tables.projects
    found = await conn.execute(select(projects.c.id).where(projects.c.name == name).with_for_update())
    return found.scalar_one() if one else found.scalar()


async def insert_project(conn: AsyncConnection, name: str, created_by: int, fields: dict) -> int | None:
    """Insert project ``name``; its id, or None when another request registered it a moment ago."""
    projects = tables.projects
    inserted = await conn.execute(
        pg_insert(projects)
        .values(name=name, created_by=created_by, **fields)
        .on_conflict_do_nothing(index_elements=[projects.c.name])
        .returning(projects.c.id)
    )
    return inserted.scalar()


async def grant_role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    """The role of the grant user ``user_id`` holds on project ``project_id``, or None."""
    grants = tables.grants
    return (
        await conn.execute(select(grants.c.role).where(grants.c.user_id == user_id, grants.c.project_id == project_id))
    ).scalar()


async def update_project(conn: AsyncConnection, project_id: int, fields: dict) -> None:
    projects = tables.projects
    await conn.execute(update(projects).values(**fields, updated_at=func.now()).where(projects.c.id == project_id))
