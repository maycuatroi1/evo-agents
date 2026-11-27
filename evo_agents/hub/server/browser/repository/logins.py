"""What a login session of a browser identity reads: one of it that has not ended, and the version of an agent's
profile it runs under."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs, tables


async def open_session(conn: AsyncConnection, identity_id: int) -> int | None:
    """The id of a login session of the identity that has not ended, if there is one."""
    r = tables.runs
    query = select(r.c.id).where(
        r.c.kind == browser_login.KIND,
        r.c.browser_identities.contains([identity_id]),
        r.c.state.not_in(runs.TERMINAL_STATES),
    )
    return (await conn.execute(query)).scalars().first()


async def profile_version(conn: AsyncConnection, profile_id: int) -> int | None:
    ap = tables.agent_profiles
    return await conn.scalar(select(ap.c.version).where(ap.c.id == profile_id))
