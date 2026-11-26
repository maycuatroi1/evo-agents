"""The SQL of the evidence of a claim: how many pieces it holds, a new piece, the row lock of one, and the marks of a
piece withdrawn or confirmed. A piece is never changed otherwise nor deleted.
"""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def count(conn: AsyncConnection, claim_id: int) -> int:
    """How many pieces of evidence claim ``claim_id`` holds, withdrawn ones included."""
    e = tables.canvas.evidence
    return int(await conn.scalar(select(func.count()).select_from(e).where(e.c.claim_id == claim_id)) or 0)


async def add(conn: AsyncConnection, values: dict) -> int:
    """Keep a piece of evidence of the columns ``values``; its id."""
    e = tables.canvas.evidence
    return int(await conn.scalar(insert(e).values(**values).returning(e.c.id)) or 0)


async def lock(conn: AsyncConnection, claim_id: int, evidence_id: int) -> None:
    """Take the row lock of evidence ``evidence_id`` of claim ``claim_id``, when it has one."""
    e = tables.canvas.evidence
    await conn.execute(select(e.c.id).where(e.c.id == evidence_id, e.c.claim_id == claim_id).with_for_update())


async def withdraw(conn: AsyncConnection, evidence_id: int, user_id: int | None, reason: str) -> None:
    e = tables.canvas.evidence
    values = {"withdrawn_at": func.now(), "withdrawn_by": user_id, "withdrawn_reason": reason}
    await conn.execute(update(e).where(e.c.id == evidence_id).values(**values))


async def confirm(conn: AsyncConnection, evidence_id: int, user_id: int | None) -> None:
    e = tables.canvas.evidence
    values = {"confirmed_by": user_id, "confirmed_at": func.now()}
    await conn.execute(update(e).where(e.c.id == evidence_id).values(**values))
