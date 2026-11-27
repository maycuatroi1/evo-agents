"""The seller's revenue (``evo_agents.hub.server.seller.service.keys`` says what each route does): the orders paid in
a month by publisher, split by ``evo_agents.hub.payments``, and a publisher's share marked paid out."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import payments
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.seller.models.seller import Payout, PayoutIn, Revenue, RevenueLine
from evo_agents.hub.server.seller.repository import revenue as repository
from evo_agents.hub.server.seller.service.shared import record, usd


def month_bounds(month: str) -> tuple[datetime, datetime]:
    year, number = (int(part) for part in month.split("-"))
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=UTC)
    return start, end


async def revenue_of(conn: AsyncConnection, month: str) -> Revenue:
    """The revenue of ``month``: the orders paid in it (UTC), by publisher, split, with what was paid out."""
    start, end = month_bounds(month)
    outs = await repository.payouts(conn, start.date())
    lines, totals = [], [Decimal(0), Decimal(0), Decimal(0)]
    for row in await repository.gross_by_publisher(conn, start, end):
        gross = payments.usd(row.gross)
        share, platform = payments.split(gross)
        out = outs.get(row.publisher)
        lines.append(
            RevenueLine(
                publisher=row.publisher,
                orders=row.orders,
                gross_usd=float(gross),
                publisher_usd=float(share),
                platform_usd=float(platform),
                paid_out=None
                if out is None
                else Payout(
                    publisher_usd=usd(out.publisher_usd), paid_by=out.login, paid_at=out.paid_at, note=out.note
                ),
            )
        )
        totals = [totals[0] + gross, totals[1] + share, totals[2] + platform]
    return Revenue(
        month=month,
        publishers=lines,
        gross_usd=float(totals[0]),
        publisher_usd=float(totals[1]),
        platform_usd=float(totals[2]),
    )


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def month_revenue(engine: AsyncEngine, month: str | None) -> Revenue:
    """The revenue of ``month`` (this month in UTC when None), as ``revenue_of`` reads it, in a transaction of its
    own."""
    async with UnitOfWork(engine) as conn:
        return await revenue_of(conn, month or this_month())


def check_over(month: str) -> None:
    """409 while ``month`` is not over."""
    _, end = month_bounds(month)
    if end > datetime.now(UTC):
        raise Conflict(f"{month} is not over yet: its revenue can still grow")


async def mark_paid_out(engine: AsyncEngine, body: PayoutIn, user: Principal) -> Revenue:
    """Mark the share of ``body.publisher`` for ``body.month`` paid out, once."""
    async with UnitOfWork(engine) as conn:
        start, _ = month_bounds(body.month)
        found = await revenue_of(conn, body.month)
        line = next((line for line in found.publishers if line.publisher == body.publisher), None)
        if line is None:
            raise NotFound(f"publisher {body.publisher} has no revenue in {body.month}")
        if line.paid_out is not None:
            raise Conflict(f"the share of {body.publisher} for {body.month} is marked paid out already")
        gross = payments.usd(line.gross_usd)
        share, platform = payments.split(gross)
        await repository.add_payout(
            conn,
            month=start.date(),
            publisher=body.publisher,
            gross_usd=gross,
            publisher_usd=share,
            platform_usd=platform,
            paid_by=user.user_id,
            note=body.note,
        )
        await record(conn, user, audit.MARKET_PAYOUT, f"payout:{body.month} publisher:{body.publisher} usd={share}")
        return await revenue_of(conn, body.month)
