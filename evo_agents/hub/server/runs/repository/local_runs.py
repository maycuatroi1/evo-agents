"""The SQL of local runs (``evo_agents.hub.server.local_runs``): the member's run, locked; its registration, state and
end; its decisions taken or answered on the spot; and the silent runs the reaper ends."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def owned_row(conn: AsyncConnection, project_id: int, run_id: int):
    """state, event_seq, events_acked, project_id, name (the project's), plan_id, plan_revision, step_key,
    dispatched_by and kind of local run ``run_id`` of project ``project_id``, its row locked; None for no such run."""
    r, p = tables.runs, tables.projects
    query = (
        select(
            r.c.state,
            r.c.event_seq,
            r.c.events_acked,
            r.c.project_id,
            p.c.name,
            r.c.plan_id,
            r.c.plan_revision,
            r.c.step_key,
            r.c.dispatched_by,
            r.c.kind,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id, r.c.project_id == project_id, r.c.kind == "local")
        .with_for_update(of=r)
    )
    return (await conn.execute(query)).one_or_none()


async def writing_row(conn: AsyncConnection, project_id: int, run_id: int):
    """(dispatched_by,) of local run ``run_id`` of project ``project_id``, its row locked, or None."""
    r = tables.runs
    query = (
        select(r.c.dispatched_by)
        .where(r.c.id == run_id, r.c.project_id == project_id, r.c.kind == "local")
        .with_for_update()
    )
    return (await conn.execute(query)).one_or_none()


async def held_plan(conn: AsyncConnection, run_id: int):
    """(plan_id, step_key) of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.plan_id, r.c.step_key).where(r.c.id == run_id))).one()


async def set_run(conn: AsyncConnection, run_id: int, values: dict) -> None:
    r = tables.runs
    await conn.execute(update(r).values(**values).where(r.c.id == run_id))


async def touch(conn: AsyncConnection, run_id: int) -> None:
    await set_run(conn, run_id, {"last_activity_at": func.now()})


async def end_run(conn: AsyncConnection, run_id: int, decisions: str) -> None:
    """Run ``run_id`` ends, and its open decisions become ``decisions``."""
    d = tables.decisions
    await set_run(conn, run_id, {"state": "ended", "finished_at": func.now(), "waiting_since": None})
    await conn.execute(update(d).values(state=decisions).where(d.c.run_id == run_id, d.c.state == "open"))


async def insert_local(conn: AsyncConnection, values: dict) -> int | None:
    """The id of the local run made with ``values``, or None when its session is registered already."""
    r = tables.runs
    made = (
        pg_insert(r)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[r.c.project_id, r.c.session_id], index_where=r.c.kind == "local")
        .returning(r.c.id)
    )
    return (await conn.execute(made)).scalar_one_or_none()


async def held_session(conn: AsyncConnection, project_id: int, session_id: str):
    """(id, state, dispatched_by) of the local run of session ``session_id``, its row locked."""
    r = tables.runs
    held = select(r.c.id, r.c.state, r.c.dispatched_by).where(
        r.c.project_id == project_id, r.c.session_id == session_id, r.c.kind == "local"
    )
    return (await conn.execute(held.with_for_update())).one()


async def decision_row(conn: AsyncConnection, run_id: int, decision_id: int):
    """(state, delivered_at) of decision ``decision_id`` of run ``run_id``, or None."""
    d = tables.decisions
    found = select(d.c.state, d.c.delivered_at).where(d.c.id == decision_id, d.c.run_id == run_id)
    return (await conn.execute(found)).one_or_none()


async def mark_taken(conn: AsyncConnection, run_id: int, decision_id: int) -> None:
    """The machine has the answer of decision ``decision_id``: it and its inbox message are delivered."""
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(
        update(d)
        .values(delivered_at=func.now())
        .where(d.c.id == decision_id, d.c.state == "answered", d.c.delivered_at.is_(None))
    )
    inbox = update(i).values(delivered_at=func.now())
    await conn.execute(inbox.where(i.c.run_id == run_id, i.c.decision_id == decision_id, i.c.delivered_at.is_(None)))


async def locked_decision(conn: AsyncConnection, run_id: int, decision_id: int):
    """(state, options, step_key, answered_by, login of who answered) of decision ``decision_id`` of run ``run_id``,
    its row locked, or None."""
    d, u = tables.decisions, tables.users
    locked = (
        select(d.c.state, d.c.options, d.c.step_key, d.c.answered_by, u.c.login)
        .join_from(d, u, u.c.id == d.c.answered_by, isouter=True)
        .where(d.c.id == decision_id, d.c.run_id == run_id)
        .with_for_update(of=d)
    )
    return (await conn.execute(locked)).one_or_none()


async def answer(conn: AsyncConnection, decision_id: int, user_id: int, option: str | None, text: str | None) -> None:
    """Decision ``decision_id`` is answered by ``user_id`` and delivered, and that member's notices of it are read."""
    d, n = tables.decisions, tables.notifications
    answered = {
        "state": "answered",
        "answer_option": option,
        "answer_text": text,
        "answered_by": user_id,
        "answered_at": func.now(),
        "delivered_at": func.now(),
    }
    await conn.execute(update(d).values(**answered).where(d.c.id == decision_id))
    read = update(n).values(read_at=func.now())
    await conn.execute(read.where(n.c.decision_id == decision_id, n.c.user_id == user_id, n.c.read_at.is_(None)))


async def open_decisions(conn: AsyncConnection, run_id: int) -> int:
    d = tables.decisions
    still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    return (await conn.execute(still_open)).scalar_one()


async def run_statement(conn: AsyncConnection, statement) -> None:
    """Run ``statement``, one the caller built (``run_events.deliver``)."""
    await conn.execute(statement)


async def rows_of(conn: AsyncConnection, statement) -> list:
    """The rows of ``statement``, one the caller built (``run_events.undelivered``)."""
    return list((await conn.execute(statement)).all())


def _silent(silent_for: int):
    r = tables.runs
    cutoff = func.now() - timedelta(seconds=silent_for)
    return (r.c.kind == "local", r.c.state != "ended", r.c.last_activity_at < cutoff)


async def silent_runs(conn: AsyncConnection, silent_for: int, batch: int) -> list[int]:
    """The ids of at most ``batch`` local runs, not ended, that sent nothing for ``silent_for`` seconds."""
    r = tables.runs
    query = select(r.c.id).where(*_silent(silent_for)).order_by(r.c.id).limit(batch)
    return [row.id for row in await conn.execute(query)]


async def lock_silent(conn: AsyncConnection, run_id: int, silent_for: int):
    """(state,) of run ``run_id`` locked while it is still silent, or None when it spoke or another holds it."""
    r = tables.runs
    locked = select(r.c.state).where(r.c.id == run_id, *_silent(silent_for)).with_for_update(skip_locked=True)
    return (await conn.execute(locked)).one_or_none()
