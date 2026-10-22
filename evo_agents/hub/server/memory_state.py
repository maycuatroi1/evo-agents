"""What a memory shows of its upkeep (plan agent-profile-workspace, criterion a3), for ``evo_agents.hub.server.memories``:
when the agent of a run last opened it, when the daily job archived it, and when that job found it stale
(``evo_agents.hub.server.memory_upkeep``). The three are an agent's memory's alone; a memory of no agent shows none.

The agent of a run opening an agent's memory with memory_get counts as a read (``opened``); a memory listed in the
index of a prompt, or found by a search, does not. A new revision of a memory, whoever writes it, clears its archive and
its stale mark (``FRESH``): the job reads it again the next day.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, bindparam, func, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.security import Principal


class MemoryState(BaseModel):
    read_at: datetime | None = Field(None, description="an agent's memory: when the agent of a run last opened it")
    archived_at: datetime | None = Field(
        None,
        description="an agent's memory: when the hub archived it, nobody having opened it for 30 days; out of the "
        "index and the searches of runs until a writer restores it",
    )
    stale_at: datetime | None = Field(
        None, description="an agent's memory: since when a path it cites (code:REPO:PATH) is gone from the graph"
    )


def columns() -> tuple:
    """The columns of memories a MemoryState shows."""
    m = tables.memories
    return m.c.read_at, m.c.archived_at, m.c.stale_at


FRESH = {"archived_at": None, "stale_at": None}  # what a new revision sets

_OPENED = (
    update(tables.memories)
    .values(read_at=func.now())
    .where(tables.memories.c.id == bindparam("opened_id", type_=BigInteger))
)


async def opened(conn: AsyncConnection, user: Principal, profile_id: int | None, memory_id: int) -> None:
    """The read of memory ``memory_id``, of namespace ``profile_id``, that ``user`` opened: recorded when the agent of
    a run opened an agent's memory."""
    if user.scope is not None and profile_id is not None:
        await conn.execute(_OPENED, {"opened_id": memory_id})
