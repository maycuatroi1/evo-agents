"""The rows the ground of the API of core plugins reads: the installed plugin, a project's id, the versions of a
plugin's config by project and the projects that enable a plugin."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_query


async def installed(conn: AsyncConnection, name: str):
    """The id and manifest of plugin ``name`` while it is installed and on, else None."""
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.name == name, p.c.disabled_reason.is_(None))
    )
    return (await conn.execute(query)).one_or_none()


async def project_id_of(conn: AsyncConnection, project: str) -> int | None:
    """The id of project ``project``, None when there is none."""
    p = tables.projects
    return (await conn.execute(select(p.c.id).where(p.c.name == project))).scalar_one_or_none()


async def config_versions(conn: AsyncConnection, plugin_row_id: int) -> dict[int, int]:
    """The latest version of the project layer of the plugin's config, by project id."""
    c = tables.plugin_configs.c
    query = (
        select(c.project_id, func.max(c.version))
        .where(c.plugin_id == plugin_row_id, c.layer == "project", c.profile_id.is_(None))
        .group_by(c.project_id)
    )
    return dict((await conn.execute(query)).all())


async def enabling_projects(conn: AsyncConnection, plugin: str):
    """The rows of the projects that enable plugin ``plugin``, by project name."""
    query = enabled_query().where(tables.plugins.c.name == plugin).order_by(tables.projects.c.name)
    return (await conn.execute(query)).all()
