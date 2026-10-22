"""The values of an agent's accounts kept out of what its runs post to the hub (``docs/credentials.md``, section
"Accounts of an agent").

The daemon replaces each lease value its runs hold by ``***`` in every event before it reaches the spool
(``evo_agents.worker.credentials.scrub``). A message an agent posts to a chat goes to the hub's route straight from
the agent's process, so the hub does the same there: ``mask_values`` opens each live account of the profile with its
own associated data (``sealing.agent_secret_aad``), whichever worker it is bound to, and replaces every occurrence of
its value in the text by ``***``, longest value first. A value that does not open with the hub's key is left out: it
could not have been leased either. Nothing here logs or keeps a value.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, agent_secret_aad

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
