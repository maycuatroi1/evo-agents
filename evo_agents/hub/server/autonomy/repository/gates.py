"""Every query of the gates of autonomy: the principal and project of a run, the refusals audited before, and the
decisions of category ``autonomy`` asked of a principal."""

from __future__ import annotations

from sqlalchemy import exists, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit


async def principal_of(conn: AsyncConnection, run_id: int):
    """(principal_id, project_id) of the run."""
    r = tables.runs
    return (await conn.execute(select(r.c.principal_id, r.c.project_id).where(r.c.id == run_id))).one()


async def refused_before(conn: AsyncConnection, target: str) -> bool:
    """Whether the audit holds an ``autonomy.refused`` row of ``target``."""
    a = tables.audit
    seen = exists().where(a.c.action == audit.AUTONOMY_REFUSED, a.c.target == target)
    return (await conn.execute(select(seen))).scalar_one()


async def last_decision(conn: AsyncConnection, run_id: int | None, key: str):
    """(id, state, answer_option, approval) of the newest autonomy decision of the run for the act ``key``; None."""
    d = tables.decisions
    query = (
        select(d.c.id, d.c.state, d.c.answer_option, d.c.approval)
        .where(d.c.run_id == run_id, d.c.category == runs.AUTONOMY_DECISION_CATEGORY, d.c.step_key == key)
        .order_by(d.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def cancel_open(conn: AsyncConnection, decision_id: int) -> None:
    """Cancel the decision while it is open."""
    d = tables.decisions
    await conn.execute(update(d).values(state="cancelled").where(d.c.id == decision_id, d.c.state == "open"))


async def add_decision(conn: AsyncConnection, values: dict) -> int:
    """Insert the decision ``values`` names; its id."""
    d = tables.decisions
    return (await conn.execute(insert(d).values(**values).returning(d.c.id))).scalar_one()
