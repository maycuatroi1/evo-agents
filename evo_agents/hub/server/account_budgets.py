"""The ``seven_day`` window of each account with a weekly budget (``accounts.weekly_budget_usd``), as
``evo_agents.hub.quota`` says: the cost of the runs that took the account since its week began, over the budget.

A run took an account when it got a lease of it, of its value or through the model gateway (``account_views``), and
counts in the week of its first such lease; its cost is the one its usage reports (``night_cost.usage_cost``), so an
estimate. ``account_choice`` weighs the window in quota-pacing, and ``account_views`` and ``account_pools`` show it in
the account's last usage (``with_budgets``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.gateway_runs import GATEWAY
from evo_agents.hub.quota import budget_window, week_bounds, with_budget
from evo_agents.hub.server.night_cost import usage_cost

TAKEN_BY = ("secret", GATEWAY)  # the leases that say a run took an account (account_views.TAKEN_BY)


async def _spent(conn: AsyncConnection, secret_id: int, since: datetime) -> float:
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


async def budget_windows(conn: AsyncConnection, secret_ids, now: datetime | None = None) -> dict[int, dict]:
    """The ``seven_day`` window of each account of ``secret_ids`` (ids, or a select of them) that has a weekly budget,
    by secret id."""
    a = tables.accounts
    rows = await conn.execute(
        select(a.c.secret_id, a.c.weekly_budget_usd, a.c.weekly_reset_at).where(
            a.c.secret_id.in_(secret_ids), a.c.weekly_budget_usd.is_not(None)
        )
    )
    now = now or datetime.now(UTC)
    found = {}
    for row in rows.all():
        start, end = week_bounds(row.weekly_reset_at, now)
        found[row.secret_id] = budget_window(
            await _spent(conn, row.secret_id, start), float(row.weekly_budget_usd), end
        )
    return found


def with_budgets(usages: dict[int, Any], windows: dict[int, dict]) -> dict[int, Any]:
    """``usages`` (accounts.last_usage by secret id) with the budget window of each account that has one."""
    merged = dict(usages)
    for secret_id, window in windows.items():
        merged[secret_id] = with_budget(usages.get(secret_id), window)
    return merged
