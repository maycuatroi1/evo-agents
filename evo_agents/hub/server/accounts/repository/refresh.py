"""The SQL of the refresh of an OAuth account's access token (``evo_agents.hub.server.accounts.service.refresh`` says
when and how): the secret's row locked for the whole refresh, the sealed set read once the lock is won, and the new
access token written as the secret's value."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.credentials.models.sealing import Sealed


async def lock_secret(conn: AsyncConnection, owner_id: int, secret_id: int):
    """(name, kind) of ``owner_id``'s live secret ``secret_id``, its row locked until the transaction ends; None when
    there is none."""
    s = tables.secrets
    return (
        await conn.execute(
            select(s.c.name, s.c.kind)
            .where(s.c.id == secret_id, s.c.owner_id == owner_id, s.c.deleted_at.is_(None))
            .with_for_update()
        )
    ).first()


async def sealed_set(conn: AsyncConnection, secret_id: int) -> Sealed | None:
    """The sealed OAuth set of account ``secret_id``, read as a statement of its own: once the lock is won it sees what
    the refresh before it stored. None when the account holds none."""
    a = tables.accounts
    sets = select(a.c.oauth_sealed, a.c.oauth_nonce, a.c.oauth_key_id).where(a.c.secret_id == secret_id)
    row = (await conn.execute(sets)).first()
    if row is None or row.oauth_sealed is None:
        return None
    return Sealed(row.oauth_sealed, row.oauth_nonce, row.oauth_key_id)


async def store_value(conn: AsyncConnection, secret_id: int, value: Sealed, expires_at: datetime) -> None:
    """Secret ``secret_id``'s value, sealed, and when it ends."""
    s = tables.secrets
    await conn.execute(
        update(s)
        .where(s.c.id == secret_id)
        .values(
            sealed=value.ciphertext,
            nonce=value.nonce,
            key_id=value.key_id,
            expires_at=expires_at,
            updated_at=func.now(),
        )
    )
