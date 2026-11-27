"""The SQL of masking: the sealed values of a profile's live accounts, and those of the leases of a run."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def profile_secrets(conn: AsyncConnection, profile_id: int) -> list:
    """(name, sealed, nonce, key_id) of each live env account of the profile that keeps a value."""
    s = tables.secrets
    rows = await conn.execute(
        select(s.c.name, s.c.sealed, s.c.nonce, s.c.key_id).where(
            s.c.profile_id == profile_id, s.c.kind == "env", s.c.deleted_at.is_(None), s.c.sealed.is_not(None)
        )
    )
    return list(rows)


async def run_leases(conn: AsyncConnection, run_id: int) -> list:
    """Each lease of the run with its sealed value, and the secret it leased with its own."""
    cl, s = tables.credential_leases, tables.secrets
    query = (
        select(
            cl.c.id,
            cl.c.sealed_value,
            cl.c.nonce,
            cl.c.key_id,
            s.c.owner_id,
            s.c.profile_id,
            s.c.name,
            s.c.kind,
            s.c.sealed,
            s.c.nonce.label("secret_nonce"),
            s.c.key_id.label("secret_key_id"),
        )
        .select_from(cl.outerjoin(s, s.c.id == cl.c.secret_id))
        .where(cl.c.run_id == run_id)
    )
    return list(await conn.execute(query))
