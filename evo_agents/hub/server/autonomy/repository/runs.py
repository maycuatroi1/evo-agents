"""Every query of the autonomy of a run: the names its sources read, the author of the message that woke a chat run,
the snapshot runs.autonomy holds, and the credential shares the run is leased under."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def login(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()


async def profile_handle(conn: AsyncConnection, profile_id: int) -> str | None:
    """The handle of the agent profile; None when there is no such one."""
    ap = tables.agent_profiles
    return (await conn.execute(select(ap.c.handle).where(ap.c.id == profile_id))).scalar_one_or_none()


async def message_author(conn: AsyncConnection, message_id: int):
    """(author_user_id, author_profile_id, run_id) of the chat message; None when there is no such one."""
    m = tables.chat_messages
    query = select(m.c.author_user_id, m.c.author_profile_id, m.c.run_id).where(m.c.id == message_id)
    return (await conn.execute(query)).one_or_none()


async def principal_and_snapshot(conn: AsyncConnection, run_id: int):
    """(principal_id, autonomy) of the run; None when there is no such one."""
    r = tables.runs
    return (await conn.execute(select(r.c.principal_id, r.c.autonomy).where(r.c.id == run_id))).one_or_none()


async def snapshot_row(conn: AsyncConnection, run_id: int):
    """(principal_id, autonomy, project_id, repo, repos, profile_id) of the run; None when there is no such one."""
    r = tables.runs
    columns = (r.c.principal_id, r.c.autonomy, r.c.project_id, r.c.repo, r.c.repos, r.c.profile_id)
    return (await conn.execute(select(*columns).where(r.c.id == run_id))).one_or_none()


async def write_snapshot(conn: AsyncConnection, run_id: int, written: dict) -> None:
    r = tables.runs
    await conn.execute(update(r).where(r.c.id == run_id).values(autonomy=written))


async def share_levels(conn: AsyncConnection, run_id: int, lending: Iterable[int] = ()) -> list[tuple[int, str | None]]:
    """(share id, level) of each credential share run ``run_id`` holds a live lease under, and of the shares
    ``lending``."""
    cl, cs = tables.credential_leases, tables.credential_shares
    held = select(cl.c.share_id).where(cl.c.run_id == run_id, cl.c.share_id.is_not(None), cl.c.revoked_at.is_(None))
    ids = {*(await conn.execute(held)).scalars(), *lending}
    if not ids:
        return []
    rows = await conn.execute(select(cs.c.id, cs.c.level).where(cs.c.id.in_(sorted(ids))).order_by(cs.c.id))
    return [(row.id, row.level) for row in rows]
