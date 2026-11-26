"""The queries of a task's blockers: which tasks are blocked (``open_manual``, ``open_decisions``, ``blocked``, which
the list of tasks filters by too), the blockers set by hand and not cleared (``manual_rows``, ``manual_row``), the open
decisions of a task's runs (``decision_rows``), and a blocker set or changed (``insert``, ``update``).

A task is blocked while it has a blocker not cleared in tasks_blockers, or while a decision of a run that belongs to it
is open. A run belongs to a task when the task links it, or when the run is of a plan step the task links (the run's
plan_id and step_key are the link's). The second kind is never a row: answering the decision ends it.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def open_manual(task_ids: list[int] | None = None) -> Select:
    """(task_id) of every blocker set by hand and not cleared, of ``task_ids`` when given."""
    b = tables.tasks.blockers
    query = select(b.c.task_id).where(b.c.cleared_at.is_(None))
    return query if task_ids is None else query.where(b.c.task_id.in_(task_ids))


def open_decisions(task_ids: list[int] | None = None) -> Select:
    """(task_id, decision_id, run_id, asked_at, dispatched_by, question) of every open decision of a run of a task,
    one row for each task and decision."""
    d, r, k = tables.decisions, tables.runs, tables.tasks.links
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    query = (
        select(
            k.c.task_id,
            d.c.id.label("decision_id"),
            d.c.run_id,
            d.c.asked_at,
            r.c.dispatched_by,
            d.c.question,
        )
        .select_from(d.join(r, r.c.id == d.c.run_id).join(k, or_(by_run, by_step)))
        .where(d.c.state == "open")
        .distinct()
    )
    return query if task_ids is None else query.where(k.c.task_id.in_(task_ids))


def blocked():
    """A condition on tasks_items.id: the task has an open blocker of either kind."""
    t = tables.tasks.items
    return or_(t.c.id.in_(open_manual()), t.c.id.in_(select(open_decisions().subquery().c.task_id)))


def _manual_query():
    b, u = tables.tasks.blockers, tables.users
    setter, waiter, cleaner, other = (
        u.alias("setter"),
        u.alias("waiter"),
        u.alias("cleaner"),
        tables.tasks.items.alias("other"),
    )
    return (
        select(
            b.c.id,
            b.c.task_id,
            b.c.reason,
            b.c.since,
            b.c.cleared_at,
            b.c.waiting_text,
            setter.c.login.label("set_by"),
            waiter.c.login.label("waiting_login"),
            cleaner.c.login.label("cleared_by"),
            other.c.number.label("waiting_number"),
        )
        .select_from(
            b.join(setter, setter.c.id == b.c.created_by)
            .outerjoin(waiter, waiter.c.id == b.c.waiting_user_id)
            .outerjoin(cleaner, cleaner.c.id == b.c.cleared_by)
            .outerjoin(other, other.c.id == b.c.waiting_task_id)
        )
        .order_by(b.c.id)
    )


async def manual_rows(conn: AsyncConnection, task_ids: list[int]) -> list:
    """The blockers set by hand on ``task_ids`` and not cleared, the oldest first."""
    b = tables.tasks.blockers
    return list(await conn.execute(_manual_query().where(b.c.task_id.in_(task_ids), b.c.cleared_at.is_(None))))


async def manual_row(conn: AsyncConnection, task_id: int, blocker_id: int):
    """Blocker ``blocker_id`` of task ``task_id``, cleared or not; None when the task has no such blocker."""
    b = tables.tasks.blockers
    return (await conn.execute(_manual_query().where(b.c.task_id == task_id, b.c.id == blocker_id))).first()


async def decision_rows(conn: AsyncConnection, task_ids: list[int]) -> list:
    """The open decisions of the runs of ``task_ids``, the oldest first, each with the login of its run's owner."""
    u = tables.users
    decisions = open_decisions(task_ids).subquery()
    asked = (
        select(decisions, u.c.login)
        .join_from(decisions, u, u.c.id == decisions.c.dispatched_by)
        .order_by(decisions.c.asked_at, decisions.c.decision_id)
    )
    return list(await conn.execute(asked))


async def insert_blocker(conn: AsyncConnection, values: dict) -> int:
    b = tables.tasks.blockers
    return (await conn.execute(insert(b).values(**values).returning(b.c.id))).scalar_one()


async def update_blocker(
    conn: AsyncConnection, blocker_id: int, values: dict, *, cleared_by: int | None = None
) -> None:
    """Set ``values`` of blocker ``blocker_id``; with ``cleared_by``, that member cleared it now."""
    b = tables.tasks.blockers
    if cleared_by is not None:
        values = {**values, "cleared_at": func.now(), "cleared_by": cleared_by}
    await conn.execute(update(b).where(b.c.id == blocker_id).values(**values))


__all__ = [
    "blocked",
    "decision_rows",
    "insert_blocker",
    "manual_row",
    "manual_rows",
    "open_decisions",
    "open_manual",
    "update_blocker",
]
