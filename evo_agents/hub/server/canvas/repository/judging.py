"""The SQL of the judging runs of a canvas: the feedback and the neutral evidence of a claim still to judge, the items
kept for a run, whether a run is a judging run or one of a canvas is still going, the items of a run with their
claims, and a verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.runs import TERMINAL_STATES
from evo_agents.hub.server.canvas.models.judged import MAX_TRIES, JudgingRow
from evo_agents.hub.tables_canvas import JUDGED_EVIDENCE_KINDS


def _judged_or_open(column, value, claim_id: int):
    """Whether claim ``claim_id`` has the item ``column == value`` judged, or before a run still going."""
    j, r = tables.canvas.judgings, tables.runs
    going = or_(j.c.verdict.is_not(None), r.c.state.not_in(TERMINAL_STATES))
    return exists(
        select(j.c.id)
        .select_from(j.outerjoin(r, r.c.id == j.c.run_id))
        .where(j.c.claim_id == claim_id, column == value, going)
    )


def _tries(column, value, claim_id: int):
    """How many judging runs ended without judging the item ``column == value`` of claim ``claim_id``."""
    j = tables.canvas.judgings
    where = (j.c.claim_id == claim_id) & (column == value) & j.c.verdict.is_(None)
    return select(func.count()).select_from(j).where(where).scalar_subquery()


async def feedback(
    conn: AsyncConnection, claim_id: int, key_id: int, since: datetime, tenant: str | None, room: int
) -> list[Any]:
    """At most ``room`` signals of key ``key_id`` with a text, measured after ``since`` (of ``tenant`` when named),
    that claim ``claim_id`` has neither judged nor before a run going, nor left out after MAX_TRIES runs; oldest
    first."""
    s, j = tables.signals, tables.canvas.judgings
    where = [
        s.c.key_id == key_id,
        s.c.text.is_not(None),
        s.c.measured_at > since,
        ~_judged_or_open(j.c.signal_id, s.c.id, claim_id),
        _tries(j.c.signal_id, s.c.id, claim_id) < MAX_TRIES,
    ]
    if tenant:
        where.append(s.c.tenant == tenant)
    query = (
        select(s.c.id, s.c.text, s.c.synthetic, s.c.via, s.c.label, s.c.measured_at)
        .where(*where)
        .order_by(s.c.measured_at, s.c.id)
        .limit(room)
    )
    return list(await conn.execute(query))


async def neutral(conn: AsyncConnection, claim_id: int, room: int) -> list[Any]:
    """At most ``room`` pieces of neutral evidence of claim ``claim_id`` of a kind judged, not withdrawn, added by a
    person or confirmed, and neither judged, before a run going, nor left out after MAX_TRIES runs; by id."""
    e, j = tables.canvas.evidence, tables.canvas.judgings
    query = (
        select(e.c.id, e.c.kind, e.c.summary, e.c.synthetic, e.c.label, e.c.created_at)
        .where(
            e.c.claim_id == claim_id,
            e.c.kind.in_(JUDGED_EVIDENCE_KINDS),
            e.c.direction == "neutral",
            e.c.withdrawn_at.is_(None),
            or_(e.c.actor == "person", e.c.confirmed_by.is_not(None)),
            ~_judged_or_open(j.c.evidence_id, e.c.id, claim_id),
            _tries(j.c.evidence_id, e.c.id, claim_id) < MAX_TRIES,
        )
        .order_by(e.c.id)
        .limit(room)
    )
    return list(await conn.execute(query))


async def judging_run(conn: AsyncConnection, run_id: int) -> bool:
    """Whether run ``run_id`` has items kept for it."""
    j = tables.canvas.judgings
    return await conn.scalar(select(j.c.id).where(j.c.run_id == run_id).limit(1)) is not None


async def going(conn: AsyncConnection, canvas_id: int) -> bool:
    """Whether a judging run of ``canvas_id`` is still going."""
    j, r = tables.canvas.judgings, tables.runs
    query = (
        select(j.c.id)
        .join_from(j, r, r.c.id == j.c.run_id)
        .where(j.c.canvas_id == canvas_id, r.c.state.not_in(TERMINAL_STATES))
        .limit(1)
    )
    return await conn.scalar(query) is not None


async def add(conn: AsyncConnection, rows: list[dict]) -> None:
    """Keep the items ``rows`` for a judging run."""
    await conn.execute(insert(tables.canvas.judgings).values(rows))


async def judgings(conn: AsyncConnection, claim_id: int) -> list[JudgingRow]:
    """What judging runs had for claim ``claim_id``, newest first."""
    j = tables.canvas.judgings
    names = [name for name in JudgingRow.__dataclass_fields__]
    query = select(*(j.c[name] for name in names)).where(j.c.claim_id == claim_id).order_by(j.c.id.desc())
    return [JudgingRow(**row._mapping) for row in await conn.execute(query)]


async def items_of_run(conn: AsyncConnection, run_id: int) -> list[Any]:
    """The items of judging run ``run_id`` with the number, cell, test card and label of their claim, by claim."""
    j, k = tables.canvas.judgings, tables.canvas.claims
    query = (
        select(
            j.c.id,
            j.c.canvas_id,
            j.c.claim_id,
            k.c.number,
            k.c.cell,
            k.c.test_card,
            k.c.label.label("claim_label"),
            j.c.source,
            j.c.kind,
            j.c.evidence_id,
            j.c.text,
            j.c.synthetic,
            j.c.rests_on_run,
            j.c.label,
            j.c.measured_at,
            j.c.verdict,
            j.c.reason,
        )
        .join_from(j, k, k.c.id == j.c.claim_id)
        .where(j.c.run_id == run_id)
        .order_by(k.c.number, j.c.id)
    )
    return list(await conn.execute(query))


async def verdict(conn: AsyncConnection, item_id: int, given: str, reason: str, evidence_id: int | None) -> None:
    """Keep the verdict ``given`` on item ``item_id`` with ``reason``, and the evidence it wrote."""
    j = tables.canvas.judgings
    values = {"verdict": given, "reason": reason, "judged_at": func.now(), "verdict_evidence_id": evidence_id}
    await conn.execute(update(j).where(j.c.id == item_id).values(**values))
