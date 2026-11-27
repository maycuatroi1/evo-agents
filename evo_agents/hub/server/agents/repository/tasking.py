"""The SQL of whether an agent takes work: which agents do, the agent types of a profile with their plugins'
manifests, and a person's role on a project."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.agents.models.tasking import HUB


async def takes_work(conn: AsyncConnection, profile_ids: list[int]) -> set[int]:
    """Those of the agents ``profile_ids`` that are not deleted and have a pool or run on the hub."""
    ap, wp = tables.agent_profiles, tables.worker_pools
    query = (
        select(ap.c.id)
        .distinct()
        .outerjoin_from(ap, wp, wp.c.agent_id == ap.c.id)
        .where(ap.c.id.in_(profile_ids), ap.c.deleted_at.is_(None), or_(wp.c.id.is_not(None), ap.c.runs_on == HUB))
    )
    return set((await conn.execute(query)).scalars())


async def type_manifests(conn: AsyncConnection, profile_id: int) -> list:
    """(name, manifest) of each agent type of the profile, with each version of its plugin the hub holds."""
    t, v = tables.project_agent_types, tables.plugin_versions
    query = select(t.c.name, v.c.manifest).join_from(t, v, v.c.plugin_id == t.c.plugin_id)
    return list(await conn.execute(query.where(t.c.profile_id == profile_id)))


async def project_role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    """The role person ``user_id`` holds on the project; None without a grant."""
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()
