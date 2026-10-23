"""The values of an agent's accounts kept out of what its runs post to the hub (``docs/credentials.md``, section
"Accounts of an agent").

The daemon replaces each lease value its runs hold by ``***`` in every event before it reaches the spool
(``evo_agents.worker.credentials.scrub``). A message an agent posts to a chat goes to the hub's route straight from
the agent's process, so the hub does the same there: ``mask_values`` opens each live account of the profile with its
own associated data (``sealing.agent_secret_aad``), whichever worker it is bound to, and replaces every occurrence of
its value in the text by ``***``, longest value first. A value that does not open with the hub's key is left out: it
could not have been leased either. Nothing here logs or keeps a value.

The floor S3 on the hub (plan autonomy-levels, criterion a10): what a run posts, its events (``run_events.store_events``,
the worker's and a local run's) and its agent's chat messages and mentions, has every value the hub leased that run
replaced by ``***`` too, whatever the daemon did first (``mask_run``): each secret of its leases, opened with its own
associated data, and each GitHub token whose sealed value the hub still keeps (until it is revoked at GitHub). A value
shorter than MIN_SECRET_LENGTH is left alone, as the daemon leaves it, so that masking does not eat ordinary words.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.log import MIN_SECRET_LENGTH
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, agent_secret_aad, lease_aad, secret_aad

MASK = "***"  # what the daemon writes for a lease value


async def mask_values(conn: AsyncConnection, sealer: Sealer | None, profile_id: int, text: str) -> str:
    """``text`` with the value of each live account of profile ``profile_id`` replaced by MASK."""
    if sealer is None:
        return text
    s = tables.secrets
    rows = await conn.execute(
        select(s.c.name, s.c.sealed, s.c.nonce, s.c.key_id).where(
            s.c.profile_id == profile_id, s.c.kind == "env", s.c.deleted_at.is_(None), s.c.sealed.is_not(None)
        )
    )
    values = []
    for row in rows:
        try:
            values.append(
                sealer.open(Sealed(row.sealed, row.nonce, row.key_id), agent_secret_aad(profile_id, row.name))
            )
        except Unsealable:
            continue
    for value in sorted(filter(None, values), key=len, reverse=True):
        text = text.replace(value, MASK)
    return text


def _open(sealer: Sealer, row) -> str | None:
    try:
        if row.sealed_value is not None:  # a GitHub token, sealed under its lease
            return sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(row.id))
        if row.sealed is None:
            return None
        aad = (
            secret_aad(row.owner_id, row.name, row.kind)
            if row.profile_id is None
            else agent_secret_aad(row.profile_id, row.name)
        )
        return sealer.open(Sealed(row.sealed, row.secret_nonce, row.secret_key_id), aad)
    except Unsealable:
        return None


async def run_values(conn: AsyncConnection, sealer: Sealer | None, run_id: int) -> list[str]:
    """Every value the hub leased run ``run_id`` that it can still open, longest first."""
    if sealer is None:
        return []
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
    values = {_open(sealer, row) for row in await conn.execute(query)}
    return sorted((value for value in values if value and len(value) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def masked(value, values: list[str]):
    """``value`` with each of ``values`` replaced by MASK, in each string at any depth."""
    if not values:
        return value
    if isinstance(value, str):
        for secret in values:
            value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {key: masked(item, values) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [masked(item, values) for item in value]
    return value


async def mask_run(conn: AsyncConnection, sealer: Sealer | None, run_id: int, text: str) -> str:
    """``text`` with every value the hub leased run ``run_id`` replaced by MASK."""
    return masked(text, await run_values(conn, sealer, run_id))
