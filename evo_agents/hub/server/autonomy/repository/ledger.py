"""Every query of the ledger S1: who a run acts for and runs as, and the decisions written before."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit


async def run_people(conn: AsyncConnection, run_id: int):
    """(principal_id, project_id, login, handle) of the run: its principal and agent profile; None without the run."""
    r, u, ap = tables.runs, tables.users, tables.agent_profiles
    query = (
        select(r.c.principal_id, r.c.project_id, u.c.login, ap.c.handle)
        .select_from(r.outerjoin(u, u.c.id == r.c.principal_id).outerjoin(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def decided_before(conn: AsyncConnection, text: str) -> bool:
    """Whether the audit holds an ``autonomy.decided`` row of target ``text``."""
    a = tables.audit
    seen = exists().where(a.c.action == audit.AUTONOMY_DECIDED, a.c.target == text)
    return (await conn.execute(select(seen))).scalar_one()
