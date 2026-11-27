"""The SQL of the waits of plan runs' agents (``evo_agents.hub.server.run_waits``): the project's last event, the
event a wait wakes on, the answer the hub gives, and the parked runs an untaken answer resumes."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


async def last_event(conn: AsyncConnection, project_id: int) -> int:
    """The id of project ``project_id``'s last hub event, 0 for none."""
    e = tables.hub_events
    last = select(func.coalesce(func.max(e.c.id), 0)).where(e.c.project_id == project_id)
    return (await conn.execute(last)).scalar_one()


async def event_for(conn: AsyncConnection, project_id: int, wake_on: dict):
    """The first event of project ``project_id`` a wait of ``wake_on`` wakes on, None while none came."""
    e = tables.hub_events
    query = (
        select(e.c.id, e.c.kind, e.c.payload, e.c.created_at)
        .where(
            e.c.project_id == project_id,
            e.c.id > int(wake_on.get("after") or 0),
            e.c.kind == wake_on["kind"],
            e.c.payload.contains(wake_on.get("contains") or {}),
        )
        .order_by(e.c.id)
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def locked_state(conn: AsyncConnection, decision_id: int) -> str:
    d = tables.decisions
    return (await conn.execute(select(d.c.state).where(d.c.id == decision_id).with_for_update())).scalar_one()


async def answer(conn: AsyncConnection, decision_id: int, text: str) -> None:
    """Answer decision ``decision_id`` as the hub, with ``text``."""
    d = tables.decisions
    await conn.execute(
        update(d).values(state="answered", answer_text=text, answered_at=func.now()).where(d.c.id == decision_id)
    )


async def to_inbox(conn: AsyncConnection, run_id: int, text: str, decision_id: int) -> None:
    """Leave ``text`` in the inbox of run ``run_id`` as from its dispatcher, naming decision ``decision_id``."""
    r, i = tables.runs, tables.run_inbox
    owner = (await conn.execute(select(r.c.dispatched_by).where(r.c.id == run_id))).scalar_one()
    await conn.execute(insert(i).values(run_id=run_id, sent_by=owner, body=text, decision_id=decision_id))


async def open_waits(conn: AsyncConnection, batch: int) -> list:
    """The open waits, at most ``batch``, soonest first, each with whether its time came (``due``)."""
    d = tables.decisions
    due = (d.c.wake_at <= func.now()).label("due")
    query = (
        select(d.c.id, d.c.project_id, d.c.plan_id, d.c.question, d.c.wake_at, d.c.wake_on, due)
        .where(d.c.category == runs.WAIT_DECISION_CATEGORY, d.c.state == "open")
        .order_by(d.c.wake_at, d.c.id)
        .limit(batch)
    )
    return list((await conn.execute(query)).all())


def _stranded():
    r, d, i = tables.runs, tables.decisions, tables.run_inbox
    return (
        r.c.state == "parked",
        r.c.kind.in_(runs.RESUME_KINDS),
        ~exists().where(d.c.run_id == r.c.id, d.c.state == "open"),
        exists().where(i.c.run_id == r.c.id, i.c.delivered_at.is_(None)),
    )


async def stranded_runs(conn: AsyncConnection, batch: int) -> list:
    """The parked runs, at most ``batch``, with no open decision and a message of their inbox no worker took."""
    r = tables.runs
    query = select(r.c.id, r.c.project_id, r.c.plan_id).where(*_stranded()).order_by(r.c.parked_at).limit(batch)
    return list((await conn.execute(query)).all())


async def lock_stranded(conn: AsyncConnection, run_id: int) -> bool:
    """Lock run ``run_id`` while it is still stranded; False when it is not, or another holds it."""
    r = tables.runs
    lock = select(r.c.id).where(r.c.id == run_id, *_stranded()).with_for_update(of=r, skip_locked=True)
    return (await conn.execute(lock)).one_or_none() is not None


async def move_untaken(conn: AsyncConnection, run_id: int, new_id: int) -> None:
    """Move the messages of run ``run_id``'s inbox no worker took to run ``new_id``."""
    i = tables.run_inbox
    await conn.execute(update(i).values(run_id=new_id).where(i.c.run_id == run_id, i.c.delivered_at.is_(None)))
