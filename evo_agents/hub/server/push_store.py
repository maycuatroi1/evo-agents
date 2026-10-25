"""The push devices of members as the hub keeps them, in SQLAlchemy Core on ``evo_agents.hub.tables`` (schema 0149):
adding, listing and removing a member's devices, which of them still reach their member, the deliveries a
notification gets on them, and the member's preference to hide details. ``evo_agents.hub.server.push`` has the routes
and the senders, ``evo_agents.hub.server.notifications`` the outbox and the job these deliveries go through.

A device reaches its member while the token that added it lives: the member's own, neither revoked nor expired
(``device_live``). Adding a device of a kind with an address another member's device has moves it: an endpoint or a
device token speaks for one member, the last who added it. Adding it again as the same member refreshes its params, its
token and, when given, its label.

Which notifications go to devices is the tier of the notification for its member (``push_tiers.interrupts``): one of
the interrupt tier gets one delivery on each device of its member that reaches them (``device_deliveries``), besides
the web's and the channels'.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, delete, exists, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_push import KINDS

MAX_DEVICES = 20  # a member's devices; adding one more is refused until one goes
# Why a delivery to a device failed, and the device went with it, when the token that added it no longer lives.
SIGN_IN_ENDED = (
    "the sign-in that added this device ended (signed out, revoked or expired): turn push on again on the device"
)


def device_live(devices=None):
    """The token that added a device of ``devices`` (push_devices by default) lives: the same member's, neither
    revoked nor expired."""
    d, t = devices if devices is not None else tables.push_devices, tables.tokens
    return exists().where(
        t.c.id == d.c.token_id,
        t.c.user_id == d.c.user_id,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )


def device_deliveries(notification_id: int, user_id: int):
    """A delivery of notification ``notification_id`` on each device of member ``user_id`` that reaches them."""
    d, dl = tables.push_devices, tables.notification_deliveries
    rows = select(literal(notification_id, BigInteger), d.c.id).where(d.c.user_id == user_id, device_live(d))
    return insert(dl).from_select([dl.c.notification_id, dl.c.device_id], rows)


def _columns():
    d = tables.push_devices
    return (d.c.id, d.c.kind, d.c.label, d.c.created_at, d.c.last_sent_at, d.c.token_id)


async def devices_of(conn: AsyncConnection, user_id: int) -> list:
    """Member ``user_id``'s devices that reach them, newest first: id, kind, label, created_at, last_sent_at and the
    token that added each."""
    d = tables.push_devices
    query = select(*_columns()).where(d.c.user_id == user_id, device_live(d)).order_by(d.c.id.desc())
    return list((await conn.execute(query)).all())


async def add_device(
    conn: AsyncConnection, *, user_id: int, token_id: int, kind: str, address: str, params: dict, label: str | None
):
    """Add member ``user_id``'s device of ``kind`` at ``address``, bound to token ``token_id``, or refresh it; another
    member's device at that address goes, and so do the member's devices that reach them no more. (its row, as
    ``devices_of`` has them, and whether it is new), or None when the member has MAX_DEVICES devices already and this
    one is not among them."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a kind of push device")
    d = tables.push_devices
    same = (d.c.kind == kind) & (d.c.address == address)
    await conn.execute(delete(d).where(same, d.c.user_id != user_id))
    await conn.execute(delete(d).where(d.c.user_id == user_id, ~device_live(d), ~same))  # reach nobody any more
    mine = select(d.c.id).where(same, d.c.user_id == user_id)
    known = (await conn.execute(mine)).scalar_one_or_none()
    if known is None:
        count = select(func.count()).select_from(d).where(d.c.user_id == user_id)
        if (await conn.execute(count)).scalar_one() >= MAX_DEVICES:
            return None
    refreshed = {"params": params, "token_id": token_id, "label": func.coalesce(label, d.c.label)}
    added = pg_insert(d).values(
        user_id=user_id, token_id=token_id, kind=kind, address=address, params=params, label=label
    )
    added = added.on_conflict_do_update(constraint="push_devices_kind_address_key", set_=refreshed)
    row = (await conn.execute(added.returning(*_columns()))).one()
    return row, known is None


async def device_of(conn: AsyncConnection, user_id: int, device_id: int):
    """Member ``user_id``'s device ``device_id`` with its address and params, or None when it is not theirs or reaches
    them no more."""
    d = tables.push_devices
    query = select(*_columns(), d.c.address, d.c.params).where(d.c.id == device_id, d.c.user_id == user_id)
    return (await conn.execute(query.where(device_live(d)))).one_or_none()


async def remove_device(conn: AsyncConnection, device_id: int, user_id: int | None = None):
    """Delete device ``device_id`` (of member ``user_id`` when given) and its deliveries; its (id, kind, login of its
    member), or None when there was none."""
    d, u = tables.push_devices, tables.users
    removed = delete(d).where(d.c.id == device_id)
    if user_id is not None:
        removed = removed.where(d.c.user_id == user_id)
    row = (await conn.execute(removed.returning(d.c.id, d.c.kind, d.c.user_id))).one_or_none()
    if row is None:
        return None
    login = (await conn.execute(select(u.c.login).where(u.c.id == row.user_id))).scalar_one()
    return row.id, row.kind, login


async def sent(conn: AsyncConnection, device_id: int) -> None:
    """A push reached device ``device_id`` now."""
    d = tables.push_devices
    await conn.execute(update(d).values(last_sent_at=func.now()).where(d.c.id == device_id))


async def hides_details(conn: AsyncConnection, user_id: int) -> bool:
    """Whether member ``user_id`` chose push notifications without details."""
    p = tables.notification_preferences
    found = select(p.c.hide_details).where(p.c.user_id == user_id)
    return bool((await conn.execute(found)).scalar_one_or_none())
