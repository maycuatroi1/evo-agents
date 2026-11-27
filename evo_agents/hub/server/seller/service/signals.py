"""The seller's signals (``evo_agents.hub.server.seller.service.keys`` says when they are written):
market.active_licenses and market.revenue_usd_month, written to EVO_HUB_SELLER_SIGNAL_PROJECT through the project's
ingest key SIGNAL_INGEST_KEY."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import payments, signals
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.seller.models.seller import (
    ACTIVE_LICENSES,
    REVENUE,
    SIGNAL_INGEST_KEY,
    SIGNAL_KEYS,
    SIGNAL_SOURCE,
)
from evo_agents.hub.server.seller.repository import revenue as revenue_repository
from evo_agents.hub.server.seller.repository import signals as repository
from evo_agents.hub.server.seller.service.revenue import month_bounds
from evo_agents.hub.server.signals.models import SignalIn

log = logging.getLogger(__name__)


async def _signal_pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the project's ingest key SIGNAL_INGEST_KEY, made on first use and owned by the project's first
    admin, with both keys registered; None when the project has no admin or revoked the key."""
    owner = await repository.first_admin(conn, project_id)
    if owner is None:
        return None
    for name, unit, description in SIGNAL_KEYS:
        await repository.register_key(
            conn,
            project_id=project_id,
            name=name,
            unit=unit,
            description=description,
            source=SIGNAL_SOURCE,
            created_by=owner,
        )
    rules = await signals.project_rules(conn, project_id)
    found = await repository.ingest_keys(conn, project_id, SIGNAL_INGEST_KEY)
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        live = await repository.add_ingest_key(
            conn,
            project_id=project_id,
            name=SIGNAL_INGEST_KEY,
            max_level=rules.default_label.get("level"),
            token_hash=signals.hash_token(signals.new_ingest_token()),  # nobody reads it: the hub writes itself
            created_by=owner,
        )
    return signals.Pusher(project_id, rules, "ingest", live.id, SIGNAL_INGEST_KEY, live.max_level)


async def sales_figures(conn: AsyncConnection, today: date) -> tuple[int, Decimal]:
    """The licences that cover ``today`` and the revenue of its month so far."""
    active = await repository.active_count(conn, today)
    start, end = month_bounds(today.strftime("%Y-%m"))
    total = await revenue_repository.paid_total(conn, start, end)
    return int(active or 0), payments.usd(total)


async def push_signals(engine: AsyncEngine, config: HubConfig) -> dict:
    """Write market.active_licenses and market.revenue_usd_month to EVO_HUB_SELLER_SIGNAL_PROJECT; what was done."""
    project = config.seller_signal_project
    if not project:
        return {"pushed": 0, "reason": "no EVO_HUB_SELLER_SIGNAL_PROJECT"}
    async with engine.begin() as conn:
        project_id = await repository.project_id(conn, project)
        if project_id is None:
            return {"pushed": 0, "reason": f"no project {project}"}
        pusher = await _signal_pusher(conn, project_id)
        if pusher is None:
            return {"pushed": 0, "reason": f"project {project} has no admin, or revoked the key {SIGNAL_INGEST_KEY}"}
        active, total = await sales_figures(conn, datetime.now(UTC).date())
        items = [
            SignalIn(key=ACTIVE_LICENSES, value=active, source=SIGNAL_SOURCE),
            SignalIn(key=REVENUE, value=float(total), source=SIGNAL_SOURCE),
        ]
        done = await signals.push(conn, pusher, items, config.signal_days)
    return {"pushed": done.accepted, "active_licenses": active, "revenue_usd_month": float(total)}


async def push_signals_logged(engine: AsyncEngine, config: HubConfig) -> None:
    """``push_signals``, whose failure the hub logs and an order's payment does not wait on."""
    if not config.seller_signal_project:
        return
    try:
        await push_signals(engine, config)
    except Exception:  # the order is paid either way; the daily job writes them again
        log.warning("the seller's signals could not be written", exc_info=True)
