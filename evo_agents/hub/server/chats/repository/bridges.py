"""The queries of the bridges of group chats (``chats.service.bridges``): their one-time codes, the bridges and the
map of their messages both ways.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def _live_session(codes):
    """The web session a code of ``codes`` is bound to is live: of the code's maker, neither revoked nor expired. As
    ``member_channels.live_session`` writes it, which this layer may not import: it reaches the chats' service."""
    t = tables.tokens
    return exists().where(
        t.c.id == codes.c.token_id,
        t.c.user_id == codes.c.user_id,
        t.c.kind == "web",  # security.WEB
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )


async def replace_codes(conn: AsyncConnection, user_id: int, chat_id: int, kind: str) -> None:
    """Delete the codes person ``user_id`` made for chat ``chat_id`` and ``kind`` that were not used, and the codes
    that expired or were used over a day ago."""
    codes = tables.chat_bridge_codes
    day_ago = func.now() - timedelta(days=1)
    mine = and_(codes.c.user_id == user_id, codes.c.chat_id == chat_id, codes.c.kind == kind)
    await conn.execute(
        delete(codes).where(
            mine | (codes.c.expires_at < day_ago), codes.c.used_at.is_(None) | (codes.c.used_at < day_ago)
        )
    )


async def insert_code(
    conn: AsyncConnection, chat_id: int, user_id: int, token_id: int | None, kind: str, code_hash: str, seconds: int
) -> datetime:
    """Keep the code of SHA-256 ``code_hash`` that bridges chat ``chat_id`` to a group of ``kind``, made by the session
    ``token_id`` of ``user_id``, for ``seconds``: when it expires."""
    codes = tables.chat_bridge_codes
    made = (
        insert(codes)
        .values(
            chat_id=chat_id,
            user_id=user_id,
            token_id=token_id,
            kind=kind,
            code_hash=code_hash,
            expires_at=func.now() + timedelta(seconds=seconds),
        )
        .returning(codes.c.expires_at)
    )
    return (await conn.execute(made)).scalar_one()


async def live_code(conn: AsyncConnection, kind: str, code_hash: str):
    """The code of ``kind`` of SHA-256 ``code_hash`` that was not used, has not expired and whose session lives, locked,
    with its maker's login: a row (id, chat_id, user_id, token_id, login), or None."""
    codes, users = tables.chat_bridge_codes, tables.users
    query = (
        select(codes.c.id, codes.c.chat_id, codes.c.user_id, codes.c.token_id, users.c.login)
        .join_from(codes, users, users.c.id == codes.c.user_id)
        .where(
            codes.c.code_hash == code_hash,
            codes.c.kind == kind,
            codes.c.used_at.is_(None),
            codes.c.expires_at > func.now(),
            _live_session(codes),
        )
        .with_for_update(of=codes)
    )
    return (await conn.execute(query)).one_or_none()


async def use_code(conn: AsyncConnection, code_id: int) -> None:
    """Mark code ``code_id`` used."""
    codes = tables.chat_bridge_codes
    await conn.execute(update(codes).values(used_at=func.now()).where(codes.c.id == code_id))


def _columns():
    b = tables.chat_bridges
    return (b.c.id, b.c.chat_id, b.c.plugin, b.c.kind, b.c.external_id, b.c.config, b.c.state, b.c.linked_at)


async def insert_bridge(
    conn: AsyncConnection, chat_id: int, plugin: str, kind: str, group: str, config: dict, linked_by: int
):
    """Bridge chat ``chat_id`` to group ``group`` of ``kind``: the bridge's row, None when the group is bridged
    already."""
    b = tables.chat_bridges
    made = (
        pg_insert(b)
        .values(chat_id=chat_id, plugin=plugin, kind=kind, external_id=group, config=config, linked_by=linked_by)
        .on_conflict_do_nothing(index_elements=[b.c.kind, b.c.external_id], index_where=b.c.unlinked_at.is_(None))
        .returning(*_columns())
    )
    return (await conn.execute(made)).one_or_none()


async def linked(conn: AsyncConnection, kind: str, *, bridge_id: int | None = None, group: str | None = None):
    """The linked bridge of ``kind`` with id ``bridge_id`` or of group ``group``, or None."""
    b = tables.chat_bridges
    query = select(*_columns()).where(b.c.kind == kind, b.c.unlinked_at.is_(None))
    if bridge_id is not None:
        query = query.where(b.c.id == bridge_id)
    if group is not None:
        query = query.where(b.c.external_id == group)
    return (await conn.execute(query)).one_or_none()


async def unlink_bridge(conn: AsyncConnection, bridge_id: int, via: str, actor_id: int | None):
    """Mark bridge ``bridge_id`` unlinked from ``via`` by ``actor_id``: a row (chat_id, kind), None when it was not
    linked."""
    b = tables.chat_bridges
    return (
        await conn.execute(
            update(b)
            .values(unlinked_at=func.now(), unlinked_by=actor_id, unlinked_via=via)
            .where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
            .returning(b.c.chat_id, b.c.kind)
        )
    ).one_or_none()


async def group_taken(conn: AsyncConnection, bridge_id: int, kind: str, group: str) -> bool:
    """Whether a linked bridge of ``kind`` other than ``bridge_id`` has group ``group``."""
    b = tables.chat_bridges
    taken = await conn.execute(
        select(b.c.id)
        .where(b.c.kind == kind, b.c.external_id == group, b.c.unlinked_at.is_(None), b.c.id != bridge_id)
        .limit(1)
    )
    return taken.first() is not None


async def move_bridge(conn: AsyncConnection, bridge_id: int, group: str) -> bool:
    """Linked bridge ``bridge_id`` now has group ``group``; whether it moved."""
    b = tables.chat_bridges
    query = update(b).values(external_id=group).where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
    return (await conn.execute(query)).rowcount == 1


async def set_state(conn: AsyncConnection, bridge_id: int, state: dict) -> bool:
    b = tables.chat_bridges
    query = update(b).values(state=state).where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
    return (await conn.execute(query)).rowcount == 1


async def carry(conn: AsyncConnection, bridge_id: int, message_id: int, direction: str, group_message: str) -> None:
    """Map hub message ``message_id`` to the group's message ``group_message``, which it came ``in`` from or went
    ``out`` as; a mapping there is already stays."""
    bm = tables.chat_bridge_messages
    await conn.execute(
        pg_insert(bm)
        .values(bridge_id=bridge_id, message_id=message_id, direction=direction, external_id=group_message)
        .on_conflict_do_nothing()
    )


async def hub_message(conn: AsyncConnection, bridge_id: int, group_message: str) -> int | None:
    """The hub message the group's message ``group_message`` came in as or went out from, on bridge ``bridge_id``."""
    bm = tables.chat_bridge_messages
    query = select(bm.c.message_id).where(bm.c.bridge_id == bridge_id, bm.c.external_id == group_message)
    return (await conn.execute(query)).scalar_one_or_none()


async def group_message(conn: AsyncConnection, bridge_id: int, message_id: int) -> str | None:
    """The group's message hub message ``message_id`` came in from or went out as on bridge ``bridge_id``."""
    bm = tables.chat_bridge_messages
    query = (
        select(bm.c.external_id)
        .where(bm.c.bridge_id == bridge_id, bm.c.message_id == message_id)
        .order_by(bm.c.direction)
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def has_bridge(conn: AsyncConnection, chat_id: int, bridge_id: int) -> bool:
    """Whether bridge ``bridge_id`` of chat ``chat_id`` is linked."""
    b = tables.chat_bridges
    query = select(b.c.id).where(b.c.id == bridge_id, b.c.chat_id == chat_id, b.c.unlinked_at.is_(None))
    return (await conn.execute(query)).first() is not None
