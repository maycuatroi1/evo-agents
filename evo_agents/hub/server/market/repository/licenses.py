"""The SQL of buying a paid plugin: the hub's stable id, its purchases and their licences, the login of who placed
one, and turning off a plugin whose licence lapsed."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, tables_seller


async def hub_id(conn: AsyncConnection) -> str:
    """The hub's stable id, made the first time it is asked for."""
    h = tables.hub_identity
    await conn.execute(pg_insert(h).values(hub_id=f"hub_{secrets.token_hex(16)}").on_conflict_do_nothing())
    return await conn.scalar(select(h.c.hub_id))  # pyright: ignore[reportReturnType]


def _purchases_query():
    p, users = tables.market_purchases, tables.users
    return select(p, users.c.login).join_from(p, users, users.c.id == p.c.placed_by, isouter=True)


async def purchase(conn: AsyncConnection, purchase_id: int):
    """Purchase ``purchase_id`` with the login of who placed it."""
    p = tables.market_purchases
    return (await conn.execute(_purchases_query().where(p.c.id == purchase_id))).one()


async def purchases(conn: AsyncConnection) -> Sequence[Any]:
    """The hub's purchases with the login of who placed each, the newest first."""
    p = tables.market_purchases
    return (await conn.execute(_purchases_query().order_by(p.c.id.desc()))).all()


async def add_purchase(conn: AsyncConnection, **values) -> int:
    p = tables.market_purchases
    made = await conn.execute(insert(p).values(**values).returning(p.c.id))
    return made.scalar_one()


async def paid_of(conn: AsyncConnection, plugin: str) -> Sequence[Any]:
    """The paid purchases of plugin ``plugin``."""
    p = tables.market_purchases
    return (await conn.execute(select(p).where(p.c.plugin == plugin, p.c.state == "paid"))).all()


async def paid(conn: AsyncConnection) -> Sequence[Any]:
    """Every paid purchase, by plugin then by the end of its licence."""
    p = tables.market_purchases
    return (await conn.execute(select(p).where(p.c.state == "paid").order_by(p.c.plugin, p.c.valid_until))).all()


async def open_purchases(conn: AsyncConnection) -> Sequence[Any]:
    p = tables.market_purchases
    return (await conn.execute(select(p).where(p.c.state == "open").order_by(p.c.id))).all()


async def note(conn: AsyncConnection, purchase_id: int, error: str | None) -> None:
    """Mark purchase ``purchase_id`` checked now, with ``error`` (cut to its column) or none."""
    p = tables.market_purchases
    await conn.execute(
        update(p)
        .where(p.c.id == purchase_id)
        .values(checked_at=func.now(), last_error=None if error is None else error[: tables_seller.MAX_ERROR_CHARS])
    )


async def refuse(conn: AsyncConnection, purchase_id: int, error: str) -> None:
    p = tables.market_purchases
    await conn.execute(update(p).where(p.c.id == purchase_id).values(checked_at=func.now(), last_error=error))


async def cancel(conn: AsyncConnection, purchase_id: int) -> None:
    p = tables.market_purchases
    await conn.execute(
        update(p).where(p.c.id == purchase_id).values(state="cancelled", checked_at=func.now(), last_error=None)
    )


async def keep_licence(conn: AsyncConnection, purchase_id: int, granted, token: str) -> None:
    """Purchase ``purchase_id`` paid, with licence ``granted`` and its token."""
    p = tables.market_purchases
    await conn.execute(
        update(p)
        .where(p.c.id == purchase_id)
        .values(
            state="paid",
            license_id=granted.license,
            valid_from=granted.valid_from,
            valid_until=granted.valid_until,
            key_id=granted.key,
            token=token,
            received_at=func.now(),
            checked_at=func.now(),
            last_error=None,
        )
    )


async def mark_reminded(conn: AsyncConnection, purchase_id: int) -> None:
    p = tables.market_purchases
    await conn.execute(update(p).where(p.c.id == purchase_id).values(reminded_at=func.now()))


async def login_of(conn: AsyncConnection, user_id: int) -> str | None:
    users = tables.users
    return await conn.scalar(select(users.c.login).where(users.c.id == user_id))


async def turn_off(conn: AsyncConnection, plugin_id: int, reason: str) -> None:
    """Turn plugin ``plugin_id`` off on the whole hub, for ``reason``."""
    plugins = tables.plugins
    await conn.execute(
        update(plugins).where(plugins.c.id == plugin_id).values(disabled_reason=reason, disabled_at=func.now())
    )
