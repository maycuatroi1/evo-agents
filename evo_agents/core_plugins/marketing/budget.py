"""The monthly API budget of a project (``docs/marketing-plugin.md``): 50 USD unless the config says another amount.

A post on a paid channel reserves what it is expected to cost before it goes out, and settles what it did cost after.
When spent plus reserved plus the new post would pass the budget, the post is not reserved: it becomes a draft with the
reason, and every paid channel stays stopped until the next calendar month (UTC). Free channels cost nothing and are
never stopped by it. When the spend ends above the budget (a platform charged more than expected) the project's kill
switch is set (``breaker``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import tables


def month_of(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


async def _locked(conn: AsyncConnection, project_id: int, month: str):
    t = tables.marketing_budget
    await conn.execute(pg_insert(t).values(project_id=project_id, month=month).on_conflict_do_nothing())
    query = select(t.c.reserved_usd, t.c.spent_usd).where(t.c.project_id == project_id, t.c.month == month)
    return (await conn.execute(query.with_for_update())).one()


async def status(conn: AsyncConnection, project_id: int, month: str) -> tuple[Decimal, Decimal]:
    """(reserved, spent) in the month, in USD."""
    t = tables.marketing_budget.c
    found = (
        await conn.execute(select(t.reserved_usd, t.spent_usd).where(t.project_id == project_id, t.month == month))
    ).first()
    return (found.reserved_usd, found.spent_usd) if found else (Decimal(0), Decimal(0))


async def reserve(conn: AsyncConnection, project_id: int, month: str, amount: float, limit: float) -> bool:
    """Reserve ``amount`` of the month's budget; False when it would pass ``limit``, nothing reserved."""
    cost = _money(amount)
    if cost <= 0:
        return True
    row = await _locked(conn, project_id, month)
    if row.spent_usd + row.reserved_usd + cost > _money(limit):
        return False
    t = tables.marketing_budget
    await conn.execute(
        update(t).where(t.c.project_id == project_id, t.c.month == month).values(reserved_usd=row.reserved_usd + cost)
    )
    return True


async def settle(conn: AsyncConnection, project_id: int, month: str, reserved: float, actual: float) -> Decimal:
    """Turn a reservation into what the post cost (0 when it did not go out); the month's spend after."""
    row = await _locked(conn, project_id, month)
    spent = row.spent_usd + _money(actual)
    t = tables.marketing_budget
    await conn.execute(
        update(t)
        .where(t.c.project_id == project_id, t.c.month == month)
        .values(reserved_usd=max(Decimal(0), row.reserved_usd - _money(reserved)), spent_usd=spent)
    )
    return spent
