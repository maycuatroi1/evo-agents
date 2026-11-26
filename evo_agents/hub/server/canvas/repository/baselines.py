"""The SQL of the baselines of a canvas: a baseline and its items as rows, the latest approved snapshot, the open
proposals of the value check, the revs that moved a claim's status, and the writes of proposing, expiring, deciding
and approving a baseline.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.canvas.models.baseline_rows import BaselineRow, ItemRow


def _baseline_query():
    b, users = tables.canvas.baselines, tables.users
    return select(
        b.c.id,
        b.c.canvas_id,
        b.c.number,
        b.c.kind,
        b.c.week,
        b.c.due_at,
        b.c.time_zone,
        b.c.status,
        b.c.label,
        users.c.login.label("created_by"),
        b.c.created_at,
        b.c.approved_at,
        b.c.expired_at,
        b.c.snapshot,
    ).select_from(b.outerjoin(users, users.c.id == b.c.created_by))


async def baselines_of(conn: AsyncConnection, canvas_id: int, limit: int) -> list[BaselineRow]:
    """The baselines of canvas ``canvas_id``, newest first."""
    b = tables.canvas.baselines
    query = _baseline_query().where(b.c.canvas_id == canvas_id).order_by(b.c.number.desc()).limit(limit)
    return [BaselineRow(**row._mapping) for row in await conn.execute(query)]


async def baseline_of(conn: AsyncConnection, canvas_id: int, number: int, *, lock: bool = False) -> BaselineRow | None:
    """Baseline ``number`` of canvas ``canvas_id``; None when it has none. ``lock`` takes its row lock."""
    b = tables.canvas.baselines
    where = (b.c.canvas_id == canvas_id) & (b.c.number == number)
    if lock:
        await conn.execute(select(b.c.id).where(where).with_for_update())
    row = (await conn.execute(_baseline_query().where(where))).first()
    return None if row is None else BaselineRow(**row._mapping)


async def items(conn: AsyncConnection, baseline_id: int) -> list[ItemRow]:
    """The items of a baseline, by claim number."""
    i, k, users = tables.canvas.baseline_items, tables.canvas.claims, tables.users
    query = (
        select(
            i.c.id,
            i.c.claim_id,
            k.c.number,
            i.c.kind,
            i.c.from_status,
            i.c.at_status,
            i.c.to_status,
            i.c.claim_rev,
            i.c.proposal_id,
            i.c.deadline,
            i.c.evidence_ids,
            i.c.proposers,
            i.c.label,
            i.c.decision,
            users.c.login.label("decided_by"),
            i.c.decided_at,
            i.c.new_deadline,
            i.c.reason,
            i.c.rev,
        )
        .select_from(i.join(k, k.c.id == i.c.claim_id).outerjoin(users, users.c.id == i.c.decided_by))
        .where(i.c.baseline_id == baseline_id)
        .order_by(k.c.number)
    )
    return [ItemRow(**row._mapping) for row in await conn.execute(query)]


async def logins(conn: AsyncConnection, user_ids) -> dict[int, str]:
    users = tables.users
    found = await conn.execute(select(users.c.id, users.c.login).where(users.c.id.in_(sorted(set(user_ids)))))
    return {int(user_id): str(login) for user_id, login in found.all()}


async def latest_snapshot(conn: AsyncConnection, canvas_id: int) -> list | None:
    """The snapshot of the latest approved baseline of canvas ``canvas_id``; None before the first approval."""
    b = tables.canvas.baselines
    query = (
        select(b.c.snapshot)
        .where(b.c.canvas_id == canvas_id, b.c.status == "approved")
        .order_by(b.c.approved_at.desc(), b.c.id.desc())
        .limit(1)
    )
    return await conn.scalar(query)


async def today(conn: AsyncConnection) -> date:
    """The day it is in UTC by Postgres's clock."""
    now = await conn.scalar(select(func.timezone("UTC", func.now())))
    if now is None:
        raise RuntimeError("Postgres gave no time")
    return now.date()


async def value_checks(conn: AsyncConnection, canvas_id: int) -> dict[int, tuple[int, str]]:
    """claim id -> (the open proposal of the value check, the status it asks) of the claims of a canvas."""
    p = tables.canvas.proposals
    query = (
        select(p.c.claim_id, p.c.id, p.c.fields["status"].astext)
        .where(
            p.c.canvas_id == canvas_id,
            p.c.status == "open",
            p.c.actor == "hub",
            p.c.kind == "change",
            p.c.evidence_id.is_not(None),
            p.c.fields["status"].astext.is_not(None),
        )
        .order_by(p.c.id)
    )
    return {
        int(claim_id): (int(proposal_id), str(status)) for claim_id, proposal_id, status in await conn.execute(query)
    }


async def status_revs(conn: AsyncConnection, claim_id: int, from_rev: int) -> list[Any]:
    """(rev, status, change, member id) of each rev of claim ``claim_id`` from ``from_rev`` on, by rev."""
    r = tables.canvas.revs
    query = (
        select(r.c.rev, r.c.content["status"].astext, r.c.change, r.c.user_id)
        .where(r.c.claim_id == claim_id, r.c.rev >= from_rev)
        .order_by(r.c.rev)
    )
    return list(await conn.execute(query))


async def next_number(conn: AsyncConnection, canvas_id: int) -> int:
    b = tables.canvas.baselines
    return int(await conn.scalar(select(func.max(b.c.number)).where(b.c.canvas_id == canvas_id)) or 0) + 1


async def expire_open(conn: AsyncConnection, canvas_id: int) -> int:
    """Expire every baseline of canvas ``canvas_id`` still proposed; how many."""
    b = tables.canvas.baselines
    moved = update(b).where(b.c.canvas_id == canvas_id, b.c.status == "proposed")
    return int((await conn.execute(moved.values(status="expired", expired_at=func.now()))).rowcount or 0)


async def add(conn: AsyncConnection, values: dict) -> int:
    """Keep a baseline of the columns ``values``; its id."""
    b = tables.canvas.baselines
    return int(await conn.scalar(insert(b).values(**values).returning(b.c.id)) or 0)


async def now(conn: AsyncConnection) -> Any:
    """Postgres's clock: one value for every row of a batch insert, which binds no SQL function."""
    return await conn.scalar(select(func.now()))


async def add_items(conn: AsyncConnection, rows: list[dict]) -> None:
    if rows:
        await conn.execute(insert(tables.canvas.baseline_items), rows)


async def waiting_in_open(conn: AsyncConnection, claim_id: int) -> int | None:
    """The number of a baseline still proposed that holds an item of claim ``claim_id``, or None."""
    b, i = tables.canvas.baselines, tables.canvas.baseline_items
    query = (
        select(b.c.number)
        .join_from(i, b, b.c.id == i.c.baseline_id)
        .where(i.c.claim_id == claim_id, b.c.status == "proposed")
        .limit(1)
    )
    found = await conn.scalar(query)
    return None if found is None else int(found)


async def approved(conn: AsyncConnection, baseline_id: int, snapshot: list[dict]) -> None:
    """Mark baseline ``baseline_id`` approved with ``snapshot``, while it is still proposed."""
    b = tables.canvas.baselines
    values = {"status": "approved", "approved_at": func.now(), "snapshot": snapshot}
    await conn.execute(update(b).where(b.c.id == baseline_id, b.c.status == "proposed").values(**values))


async def decide_item(conn: AsyncConnection, item_id: int, user_id: int | None, values: dict) -> None:
    """Keep what member ``user_id`` decided of item ``item_id``: the columns ``values``, and when."""
    i = tables.canvas.baseline_items
    await conn.execute(update(i).where(i.c.id == item_id).values(**values, decided_by=user_id, decided_at=func.now()))
