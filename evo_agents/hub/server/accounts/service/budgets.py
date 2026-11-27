"""The ``seven_day`` window of each account with a weekly budget (``accounts.weekly_budget_usd``), as
``evo_agents.hub.quota`` says: the cost of the runs that took the account since its week began, over the budget.

A run took an account when it got a lease of it, of its value or through the model gateway (``views``), and counts in
the week of its first such lease; its cost is the one its usage reports (``night_cost.usage_cost``), so an estimate.
``choice`` weighs the window in quota-pacing, and ``views`` and ``pools`` show it in the account's last usage
(``with_budgets``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.quota import budget_window, week_bounds, with_budget
from evo_agents.hub.server.accounts.repository import budgets as repository


async def budget_windows(conn: AsyncConnection, secret_ids, now: datetime | None = None) -> dict[int, dict]:
    """The ``seven_day`` window of each account of ``secret_ids`` (ids, or a select of them) that has a weekly budget,
    by secret id."""
    rows = await repository.budgeted(conn, secret_ids)
    now = now or datetime.now(UTC)
    found = {}
    for row in rows:
        start, end = week_bounds(row.weekly_reset_at, now)
        found[row.secret_id] = budget_window(
            await repository.spent(conn, row.secret_id, start), float(row.weekly_budget_usd), end
        )
    return found


def with_budgets(usages: dict[int, Any], windows: dict[int, dict]) -> dict[int, Any]:
    """``usages`` (accounts.last_usage by secret id) with the budget window of each account that has one."""
    merged = dict(usages)
    for secret_id, window in windows.items():
        merged[secret_id] = with_budget(usages.get(secret_id), window)
    return merged
