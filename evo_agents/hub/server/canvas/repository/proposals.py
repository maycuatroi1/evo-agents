"""The SQL of the proposals of a canvas: how many are open, a new one, the proposals of a canvas and one of them with
its row lock, and the marks of a decision on them.
"""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.canvas.models.content import code
from evo_agents.hub.server.canvas.models.rows import ProposalRow
from evo_agents.hub.server.canvas.repository.store import provenance_of


async def open_count(conn: AsyncConnection, canvas_id: int) -> int:
    """How many proposals of canvas ``canvas_id`` are open."""
    p = tables.canvas.proposals
    query = select(func.count()).select_from(p).where(p.c.canvas_id == canvas_id, p.c.status == "open")
    return int(await conn.scalar(query) or 0)


async def add(conn: AsyncConnection, values: dict) -> int:
    """Keep a proposal of the columns ``values``; its id."""
    p = tables.canvas.proposals
    return int(await conn.scalar(insert(p).values(**values).returning(p.c.id)) or 0)


def _query(canvas_id: int):
    p, k, users = tables.canvas.proposals, tables.canvas.claims, tables.users
    decider = users.alias("decider")
    return (
        select(
            p.c.id,
            p.c.kind,
            p.c.claim_id,
            k.c.number,
            p.c.base_rev,
            p.c.fields,
            p.c.reason,
            p.c.synthetic,
            p.c.actor,
            users.c.login,
            p.c.run_id,
            p.c.runtime,
            p.c.model,
            p.c.chat_message_id,
            p.c.created_at,
            p.c.status,
            p.c.rev,
            decider.c.login.label("decided_by"),
            p.c.decided_at,
            p.c.decided_reason,
            p.c.evidence_id,
        )
        .select_from(
            p.outerjoin(k, k.c.id == p.c.claim_id)
            .outerjoin(users, users.c.id == p.c.user_id)
            .outerjoin(decider, decider.c.id == p.c.decided_by)
        )
        .where(p.c.canvas_id == canvas_id)
    )


def _row(found) -> ProposalRow:
    return ProposalRow(
        id=found.id,
        kind=found.kind,
        claim_id=found.claim_id,
        claim=code(found.number) if found.number is not None else None,
        base_rev=found.base_rev,
        fields=found.fields,
        reason=found.reason,
        synthetic=found.synthetic,
        provenance=provenance_of(found),
        created_at=found.created_at,
        status=found.status,
        rev=found.rev,
        decided_by=found.decided_by,
        decided_at=found.decided_at,
        decided_reason=found.decided_reason,
        evidence_id=found.evidence_id,
    )


async def proposals_of(conn: AsyncConnection, canvas_id: int, statuses: tuple[str, ...] = ()) -> list[ProposalRow]:
    """The proposals of canvas ``canvas_id`` of ``statuses`` (every status when none), newest first."""
    p = tables.canvas.proposals
    query = _query(canvas_id).order_by(p.c.id.desc())
    if statuses:
        query = query.where(p.c.status.in_(statuses))
    return [_row(found) for found in await conn.execute(query)]


async def proposal_of(
    conn: AsyncConnection, canvas_id: int, proposal_id: int, *, lock: bool = False
) -> ProposalRow | None:
    """Proposal ``proposal_id`` of canvas ``canvas_id``; None when it has none. ``lock`` takes its row lock."""
    p = tables.canvas.proposals
    if lock:
        await conn.execute(select(p.c.id).where(p.c.id == proposal_id, p.c.canvas_id == canvas_id).with_for_update())
    found = (await conn.execute(_query(canvas_id).where(p.c.id == proposal_id))).first()
    return None if found is None else _row(found)


async def decided(conn: AsyncConnection, proposal_id: int, user_id: int | None, **values) -> None:
    """Mark proposal ``proposal_id`` decided by ``user_id`` now, with ``values`` (its status, and what it wrote)."""
    p = tables.canvas.proposals
    marks = {"decided_by": user_id, "decided_at": func.now()}
    await conn.execute(update(p).where(p.c.id == proposal_id).values(**marks, **values))


async def statuses(conn: AsyncConnection, canvas_id: int, proposal_ids: list[int]) -> dict[int, str]:
    """The status of each proposal of ``proposal_ids`` canvas ``canvas_id`` has, each row locked, by id."""
    p = tables.canvas.proposals
    query = select(p.c.id, p.c.status).where(p.c.canvas_id == canvas_id, p.c.id.in_(proposal_ids)).order_by(p.c.id)
    return {row.id: row.status for row in await conn.execute(query.with_for_update())}


async def reject_open(conn: AsyncConnection, proposal_id: int, user_id: int | None, reason: str | None) -> None:
    """Mark proposal ``proposal_id`` rejected by ``user_id`` now, with ``reason``, while it is still open."""
    p = tables.canvas.proposals
    marks = {"decided_by": user_id, "decided_at": func.now(), "decided_reason": reason}
    await conn.execute(update(p).where(p.c.id == proposal_id, p.c.status == "open").values(status="rejected", **marks))


async def reject_all(conn: AsyncConnection, proposal_ids: list[int], user_id: int | None, reason: str) -> None:
    """Mark proposals ``proposal_ids`` rejected by ``user_id`` now, with ``reason``."""
    p = tables.canvas.proposals
    marks = {"decided_by": user_id, "decided_at": func.now(), "decided_reason": reason}
    await conn.execute(update(p).where(p.c.id.in_(proposal_ids)).values(status="rejected", **marks))
