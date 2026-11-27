"""The SQL of the caller's own tokens (``evo_agents.hub.server.tokens``)."""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def listed(conn: AsyncConnection, user_id: int, include_inactive: bool):
    """The tokens of ``user_id``, newest first; only the live ones unless ``include_inactive``."""
    tokens = tables.tokens
    state = case(
        (tokens.c.revoked_at.is_not(None), "revoked"),
        (tokens.c.expires_at <= func.now(), "expired"),
        else_="active",
    )
    query = select(
        tokens.c.id,
        tokens.c.kind,
        tokens.c.host,
        tokens.c.created_at,
        tokens.c.last_used_at,
        tokens.c.expires_at,
        tokens.c.revoked_at,
        state.label("state"),
    ).where(tokens.c.user_id == user_id)
    if not include_inactive:
        query = query.where(tokens.c.revoked_at.is_(None), tokens.c.expires_at > func.now())
    return (await conn.execute(query.order_by(tokens.c.created_at.desc(), tokens.c.id.desc()))).all()
