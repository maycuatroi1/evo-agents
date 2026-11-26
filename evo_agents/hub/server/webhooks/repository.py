"""The SQL of outbound webhooks, in SQLAlchemy Core on ``evo_agents.hub.tables``: a project's webhooks with how many
of their deliveries wait and failed, adding, changing and deleting one, its latest deliveries, and the outbox the job
claims, settles and purges (``FOR UPDATE SKIP LOCKED``, so two passes at once never take the same delivery)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.webhooks.models import CLAIM_SECONDS, MAX_DELIVERIES_SHOWN, WebhookDelivery


def _listed(project_id: int):
    h, d, u = tables.project_webhooks, tables.webhook_deliveries, tables.users
    pending = func.count(case((d.c.state == "pending", 1)))
    failed = func.count(case((d.c.state == "failed", 1)))
    return (
        select(
            h.c.id,
            h.c.url,
            h.c.events,
            h.c.active,
            u.c.login.label("created_by"),
            h.c.created_at,
            h.c.updated_at,
            pending.label("pending"),
            failed.label("failed"),
        )
        .select_from(h.outerjoin(u, u.c.id == h.c.created_by).outerjoin(d, d.c.webhook_id == h.c.id))
        .where(h.c.project_id == project_id)
        .group_by(h.c.id, u.c.login)
        .order_by(h.c.id)
    )


async def webhooks_of(conn: AsyncConnection, project_id: int) -> list:
    """Project ``project_id``'s webhooks, each with its counts of deliveries pending and failed."""
    return list((await conn.execute(_listed(project_id))).all())


async def webhook_of(conn: AsyncConnection, project_id: int, webhook_id: int):
    """Webhook ``webhook_id`` of project ``project_id`` as ``webhooks_of`` lists it, or None."""
    h = tables.project_webhooks
    return (await conn.execute(_listed(project_id).where(h.c.id == webhook_id))).first()


async def count(conn: AsyncConnection, project_id: int) -> int:
    """How many webhooks project ``project_id`` has."""
    h = tables.project_webhooks
    held = select(func.count()).select_from(h).where(h.c.project_id == project_id)
    return (await conn.execute(held)).scalar_one()


async def add(conn: AsyncConnection, values: dict) -> int:
    """A new webhook of ``values``; its id."""
    h = tables.project_webhooks
    return (await conn.execute(insert(h).values(**values).returning(h.c.id))).scalar_one()


async def seal(conn: AsyncConnection, webhook_id: int, sealed: dict, *, touch: bool = False) -> None:
    """Webhook ``webhook_id``'s secret sealed as ``sealed``; ``touch`` moves its updated_at."""
    h = tables.project_webhooks
    values = {"sealed_secret": sealed, **({"updated_at": func.now()} if touch else {})}
    await conn.execute(update(h).values(**values).where(h.c.id == webhook_id))


async def change(conn: AsyncConnection, project_id: int, webhook_id: int, values: dict) -> None:
    """Webhook ``webhook_id`` of project ``project_id`` changed to ``values``."""
    h = tables.project_webhooks
    mine = (h.c.id == webhook_id, h.c.project_id == project_id)
    await conn.execute(update(h).values(**values, updated_at=func.now()).where(*mine))


async def remove(conn: AsyncConnection, project_id: int, webhook_id: int) -> None:
    """Webhook ``webhook_id`` of project ``project_id`` deleted, its outbox with it."""
    h = tables.project_webhooks
    await conn.execute(delete(h).where(h.c.id == webhook_id, h.c.project_id == project_id))


async def deliveries(conn: AsyncConnection, webhook_id: int) -> list:
    """Webhook ``webhook_id``'s latest deliveries, newest first, MAX_DELIVERIES_SHOWN at most."""
    d = tables.webhook_deliveries
    query = (
        select(*(d.c[name] for name in WebhookDelivery.model_fields))
        .where(d.c.webhook_id == webhook_id)
        .order_by(d.c.id.desc())
        .limit(MAX_DELIVERIES_SHOWN)
    )
    return list((await conn.execute(query)).all())


async def claim(conn: AsyncConnection):
    """One due delivery, moved CLAIM_SECONDS ahead, with its webhook and project; None when none is due."""
    d, h, p = tables.webhook_deliveries, tables.project_webhooks, tables.projects
    due = (
        select(d.c.id)
        .where(d.c.state == "pending", d.c.next_at <= func.now())
        .order_by(d.c.next_at, d.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    delivery_id = (await conn.execute(due)).scalar()
    if delivery_id is None:
        return None
    later = func.now() + timedelta(seconds=CLAIM_SECONDS)
    await conn.execute(update(d).values(next_at=later).where(d.c.id == delivery_id))
    query = (
        select(
            d.c.id,
            d.c.event,
            d.c.payload,
            d.c.attempts,
            d.c.created_at,
            h.c.id.label("webhook_id"),
            h.c.url,
            h.c.sealed_secret,
            h.c.active,
            p.c.name.label("project"),
        )
        .join_from(d, h, h.c.id == d.c.webhook_id)
        .join(p, p.c.id == h.c.project_id)
        .where(d.c.id == delivery_id)
    )
    return (await conn.execute(query)).first()


async def settle(conn: AsyncConnection, delivery_id: int, values: dict) -> None:
    """A try of delivery ``delivery_id`` written with ``values``, while it is still pending."""
    d = tables.webhook_deliveries
    await conn.execute(update(d).values(**values).where(d.c.id == delivery_id, d.c.state == "pending"))


async def purge(conn: AsyncConnection, keep_days: int) -> None:
    """The deliveries no longer pending that are older than ``keep_days`` deleted."""
    d = tables.webhook_deliveries
    old = func.now() - timedelta(days=keep_days)
    await conn.execute(delete(d).where(d.c.state != "pending", d.c.created_at < old))
