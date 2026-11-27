"""The SQL of the weekly budgets of accounts: the accounts that have one, and what the runs that took an account since
its week began cost (``evo_agents.hub.server.accounts.service.budgets`` turns them into windows)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.accounts.models.views import TAKEN_BY
from evo_agents.hub.server.night_cost import usage_cost


async def spent(conn: AsyncConnection, secret_id: int, since: datetime) -> float:
    """What the runs that first took account ``secret_id`` at ``since`` or later cost, in USD."""
    cl, r = tables.credential_leases, tables.runs
    took = (
        select(cl.c.run_id)
        .where(cl.c.provider.in_(TAKEN_BY), cl.c.secret_id == secret_id)
        .group_by(cl.c.run_id)
        .having(func.min(cl.c.issued_at) >= since)
        .subquery()
    )
    cost = select(func.coalesce(func.sum(usage_cost(r.c.usage)), 0)).join_from(took, r, r.c.id == took.c.run_id)
    return float((await conn.execute(cost)).scalar_one())


async def budgeted(conn: AsyncConnection, secret_ids) -> list:
    """The accounts of ``secret_ids`` (ids, or a select of them) that have a weekly budget: secret id, budget and the
    instant its weeks start from."""
    a = tables.accounts
    rows = await conn.execute(
        select(a.c.secret_id, a.c.weekly_budget_usd, a.c.weekly_reset_at).where(
            a.c.secret_id.in_(secret_ids), a.c.weekly_budget_usd.is_not(None)
        )
    )
    return list(rows.all())
