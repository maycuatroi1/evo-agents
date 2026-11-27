"""The SQL of the provider accounts of an agent: their rows, the one a write changes, its row locked, and the end of
its shares. Their rows are those of ``provider_store.provider_rows``, which the service passes in as ``rows_of``:
``provider_store`` reaches the service."""

from __future__ import annotations

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def provider_accounts_of(conn: AsyncConnection, rows_of, profile_id: int, name: str | None = None) -> list:
    """The rows of the profile's provider accounts (``name`` alone, when given), by name."""
    return list((await conn.execute(rows_of(tables.secrets.c.profile_id == profile_id, name))).all())


async def locked_provider_account(conn: AsyncConnection, rows_of, profile_id: int, name: str):
    """The row of the profile's provider account ``name`` with its sealed key, its secret locked; None when it has
    none."""
    s = tables.secrets
    statement = rows_of(s.c.profile_id == profile_id, name).add_columns(s.c.sealed, s.c.nonce, s.c.key_id)
    return (await conn.execute(statement.with_for_update(of=s))).one_or_none()


async def unshare(conn: AsyncConnection, secret_id: int, user_id: int) -> None:
    """Revoke the live shares of the secret, by ``user_id``."""
    cs = tables.credential_shares
    unshared = update(cs).values(revoked_at=func.now(), revoked_by=user_id)
    await conn.execute(unshared.where(cs.c.secret_id == secret_id, cs.c.revoked_at.is_(None)))
