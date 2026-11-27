"""The rows the access of a core plugin's caller reads: the login of a user."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def user_login(conn: AsyncConnection, user_id: int) -> str | None:
    """The login of user ``user_id``, None when there is none."""
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one_or_none()
