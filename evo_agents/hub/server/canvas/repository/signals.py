"""The SQL the value check reads and writes: the keys of the project's signal registry, the figures of a key's signals
and of a claim's judged feedback in a window, the newest result of a claim's check, the hub's proposals that rest on
it, and Postgres's clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def now(conn: AsyncConnection) -> datetime:
    """The time it is by Postgres's clock."""
    found = await conn.scalar(select(func.now()))
    if found is None:
        raise RuntimeError("Postgres gave no time")
    return found


async def signal_key(conn: AsyncConnection, project_id: int, name: str) -> tuple[int, str] | None:
    """(id, kind) of key ``name`` of the project's registry, archived or not; None when it has none."""
    k = tables.signal_keys
    query = select(k.c.id, k.c.kind).where(k.c.project_id == project_id, k.c.name == name)
    row = (await conn.execute(query)).first()
    return None if row is None else (int(row.id), str(row.kind))


async def number_figures(
    conn: AsyncConnection, key_id: int, since: datetime, tenant: str | None
) -> tuple[dict[str, Any], tuple[Any, ...], tuple]:
    """The figures of the signals of key ``key_id`` with a value measured after ``since`` (of ``tenant`` when named):
    the aggregates by name, (first id, last id, any synthetic, any pushed by a run), and their labels."""
    s = tables.signals
    where = [s.c.key_id == key_id, s.c.value.is_not(None), s.c.measured_at > since]
    if tenant:
        where.append(s.c.tenant == tenant)
    figures = select(
        func.count(),
        func.avg(s.c.value),
        func.sum(s.c.value),
        func.percentile_disc(0.5).within_group(s.c.value),
        func.percentile_disc(0.9).within_group(s.c.value),
        func.min(s.c.id),
        func.max(s.c.id),
        func.bool_or(s.c.synthetic),
        func.bool_or(s.c.via == "run"),
    ).where(*where)
    count, mean, total, p50, p90, first, last, synthetic, by_run = (await conn.execute(figures)).one()
    latest = await conn.scalar(select(s.c.value).where(*where).order_by(s.c.measured_at.desc(), s.c.id.desc()).limit(1))
    values = {"last": latest, "mean": mean, "sum": total, "count": count, "p50": p50, "p90": p90}
    labels = tuple(await conn.scalars(select(s.c.label).where(*where).distinct()))
    return values, (first, last, synthetic, by_run), labels


async def feedback_figures(
    conn: AsyncConnection, claim_id: int, key_id: int | None, since: datetime
) -> tuple[tuple[Any, ...], tuple]:
    """The figures of the feedback judged for claim ``claim_id`` (of key ``key_id`` when named) measured after
    ``since``: (count, supporting, first id, last id, any synthetic, any pushed by a run), and their labels."""
    j = tables.canvas.judgings
    where = [j.c.claim_id == claim_id, j.c.verdict.in_(("supports", "contradicts")), j.c.measured_at > since]
    if key_id is not None:
        where.append(j.c.key_id == key_id)
    figures = select(
        func.count(),
        func.count().filter(j.c.verdict == "supports"),
        func.min(j.c.id),
        func.max(j.c.id),
        func.bool_or(j.c.synthetic),
        func.bool_or(j.c.rests_on_run),
    ).where(*where)
    found = tuple((await conn.execute(figures)).one())
    labels = tuple(await conn.scalars(select(j.c.label).where(*where).distinct()))
    return found, labels


async def last_result(conn: AsyncConnection, claim_id: int, check: str) -> Any:
    """The newest piece of evidence the hub wrote of the claim whose basis has the key ``check``, withdrawn or not:
    id, direction, synthetic, rests_on_run, created_at, withdrawn_at; None when there is none."""
    e = tables.canvas.evidence
    query = (
        select(e.c.id, e.c.direction, e.c.synthetic, e.c.rests_on_run, e.c.created_at, e.c.withdrawn_at)
        .where(e.c.claim_id == claim_id, e.c.actor == "hub", e.c.basis.has_key(check))
        .order_by(e.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def stale_hub_proposals(conn: AsyncConnection, claim_id: int) -> None:
    """The hub's open proposals of the claim that rest on a piece of evidence go stale."""
    p = tables.canvas.proposals
    rested = (p.c.claim_id == claim_id) & (p.c.status == "open") & (p.c.actor == "hub") & p.c.evidence_id.is_not(None)
    await conn.execute(update(p).where(rested).values(status="stale"))


async def proposed_on(conn: AsyncConnection, evidence_id: int, status: str) -> bool:
    """Whether a proposal resting on evidence ``evidence_id`` asked for ``status`` already."""
    p = tables.canvas.proposals
    made = select(p.c.id).where(p.c.evidence_id == evidence_id, p.c.fields["status"].astext == status).limit(1)
    return await conn.scalar(made) is not None


async def registry(conn: AsyncConnection, project_id: int) -> list[Any]:
    """The keys of project ``project_id``'s registry not archived, by name: name, kind, unit, description, source."""
    k = tables.signal_keys
    query = (
        select(k.c.name, k.c.kind, k.c.unit, k.c.description, k.c.source)
        .where(k.c.project_id == project_id, k.c.archived_at.is_(None))
        .order_by(k.c.name)
    )
    return list(await conn.execute(query))
