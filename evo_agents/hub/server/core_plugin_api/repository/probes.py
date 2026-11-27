"""The rows the probes of a plugin's own rules read and write: the plugin's row id, a project's repo and default
label, and the retirement of a probe."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def plugin_row_id(conn: AsyncConnection, name: str) -> int:
    """The id of the row of plugin ``name``."""
    pl = tables.plugins
    return (await conn.execute(select(pl.c.id).where(pl.c.name == name))).scalar_one()


async def retire(conn: AsyncConnection, project_id: int, plugin_id: int, ref: str, note: str) -> int | None:
    """Retire the active probe of rule ``ref`` of the plugin in the project; its id, or None without one."""
    pr = tables.curator_probes
    found = await conn.execute(
        update(pr)
        .values(state="retired", retired_at=func.now(), retired_note=note, updated_at=func.now())
        .where(pr.c.project_id == project_id, pr.c.plugin_id == plugin_id, pr.c.ref == ref, pr.c.state == "active")
        .returning(pr.c.id)
    )
    return found.scalar_one_or_none()


async def has_repo(conn: AsyncConnection, project_id: int, repo: str) -> bool:
    """Whether project ``project_id`` has repo ``repo``."""
    rp = tables.project_repos
    known = await conn.execute(select(rp.c.name).where(rp.c.project_id == project_id, rp.c.name == repo))
    return known.scalar_one_or_none() is not None


async def default_label(conn: AsyncConnection, project_id: int):
    """The default label of project ``project_id``."""
    pj = tables.projects
    return (await conn.execute(select(pj.c.default_label).where(pj.c.id == project_id))).scalar_one()
