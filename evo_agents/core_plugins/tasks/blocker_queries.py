"""Which tasks are blocked: the two queries every reader of a task's blockers shares (``store`` filters by them,
``blockers`` shows them).

A task is blocked while it has a blocker not cleared in tasks_blockers, or while a decision of a run that belongs to it
is open. A run belongs to a task when the task links it, or when the run is of a plan step the task links (the run's
plan_id and step_key are the link's). The second kind is never a row: answering the decision ends it.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, or_, select

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


__all__ = ["blocked", "open_decisions", "open_manual"]
