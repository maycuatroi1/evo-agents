"""The provider accounts that may serve a member in a project (``evo_agents.hub.server.runnable_models``): their ids,
providers and models, never a key."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND


def _candidates(user_id: int, project_id: int):
    s, p, b = tables.secrets, tables.provider_accounts, tables.secret_bindings
    cs, csp = tables.credential_shares, tables.credential_share_projects
    bound = select(b.c.secret_id).where(b.c.project_id == project_id)
    shared = (
        select(cs.c.secret_id)
        .join_from(cs, csp, csp.c.share_id == cs.c.id)
        .where(csp.c.project_id == project_id, cs.c.revoked_at.is_(None))
    )
    mine = (s.c.owner_id == user_id) & s.c.id.in_(bound)
    return (
        select(s.c.id, p.c.provider, p.c.models)
        .join_from(s, p, p.c.secret_id == s.c.id)
        .where(s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None), or_(mine, s.c.id.in_(shared)))
    )


async def candidates(conn: AsyncConnection, user_id: int, project_id: int) -> list:
    """The provider accounts that may serve member ``user_id`` in the project: their own bound to it, and those shared
    for it; (id, provider, models) each."""
    return list((await conn.execute(_candidates(user_id, project_id))).all())
