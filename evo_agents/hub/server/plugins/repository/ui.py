"""The query of which bundle frames a plugin's pages in a project (``service.ui``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def enabled_version_query():
    p, v, pp, pr = tables.plugins, tables.plugin_versions, tables.project_plugins, tables.projects
    return (
        select(v.c.sha256, v.c.size, v.c.tier, v.c.signature_kind, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .join(pp, pp.c.plugin_id == p.c.id)
        .join(pr, pr.c.id == pp.c.project_id)
        .where(p.c.disabled_reason.is_(None))
    )


async def enabled_version(conn: AsyncConnection, plugin: str, project: str):
    """The installed version of plugin ``plugin``, turned on and enabled in ``project``, or None."""
    query = enabled_version_query().where(tables.plugins.c.name == plugin, tables.projects.c.name == project)
    return (await conn.execute(query)).first()
