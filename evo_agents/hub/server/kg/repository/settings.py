"""The SQL of a project's KG settings and named graphs (``evo_agents.hub.server.kg_settings``, ``kg_graphs``) beyond
their own stores: the project a run belongs to and the logins of the members who changed them."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def run_project(conn: AsyncConnection, run_id: int) -> int | None:
    """The project of run ``run_id``; None for no such run."""
    return (await conn.execute(select(tables.runs.c.project_id).where(tables.runs.c.id == run_id))).scalar()


async def logins(conn: AsyncConnection, ids: set[int]) -> dict[int, str]:
    """{user id: login} of ``ids``."""
    if not ids:
        return {}
    rows = await conn.execute(select(tables.users.c.id, tables.users.c.login).where(tables.users.c.id.in_(ids)))
    return {r.id: r.login for r in rows}
