"""Resuming a parked run (``evo_agents.hub.server.decisions``): the run that resumes it, pinned to its worker in its
session, as its owner answers one of its decisions, a wait of its agent wakes (``run_waits``) or a message reaches a
parked chat run (``run_events``); and the locks an answer takes, which a resume takes too (``lock_run_of``).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import Select

from evo_agents.hub import tables
from evo_agents.hub.server import run_access_rules
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.service.views import lock_plan
from evo_agents.hub.server.security import Principal


class _OverridingSystemValue(Select):
    """The SELECT of an INSERT ... SELECT that gives a value for runs.id, which is GENERATED ALWAYS AS IDENTITY:
    Postgres takes one only after OVERRIDING SYSTEM VALUE, which SQLAlchemy has no construct for, so this select
    renders it in front of itself. Only ``resume_parked`` needs it: it reserves the new run's id with nextval first, so
    the parked run can end done "resumed as #N" before runs_active_plan_key lets the new plan run of its plan in."""

    inherit_cache = True


@compiles(_OverridingSystemValue)
def _overriding_system_value(element, compiler, **kw):
    return "OVERRIDING SYSTEM VALUE " + compiler.visit_select(element, **kw)


def _resume_run(new_id: int, parked_id: int):
    """The run that resumes parked run ``parked_id`` (a plan run or an author run, ``runs.RESUME_KINDS``), with id
    ``new_id``, of its kind: pinned to its worker, in its session, with the agent time it used and the credential it
    was dispatched with, at the plan's current revision (the one it was dispatched from when the plan is gone; none for
    an author run that has put no plan); a run the night shift queued keeps its schedule, night and caps, an author
    run its request, a plan run its session per step, and every run its agent profile and version, and whether it runs
    as its owner (``run_access``), with a token of its own."""
    r, pl = tables.runs, tables.plans
    source = _OverridingSystemValue(
        literal(new_id, BigInteger).label("id"),
        r.c.kind,
        r.c.project_id,
        r.c.plan_id,
        r.c.title,
        func.coalesce(pl.c.revision, r.c.plan_revision).label("plan_revision"),
        r.c.dispatched_by,
        r.c.dispatched_via,
        r.c.worker_id.label("pinned_worker_id"),
        r.c.runtime.label("requested_runtime"),
        r.c.runtime,
        r.c.model,
        r.c.mode,
        r.c.approval,
        r.c.timeout_s,
        r.c.max_attempts,
        r.c.repos,
        r.c.id.label("resume_of_run_id"),
        r.c.run_seconds,
        r.c.session_id,
        r.c.schedule_id,
        r.c.schedule_night,
        r.c.budget,
        r.c.request,
        r.c.session_per_step,
        r.c.browser,
        r.c.browser_identities,
        r.c.profile_id,
        r.c.profile_version,
        r.c.principal_id,
        r.c.autonomy,
        r.c.as_owner,
        r.c.as_owner_since,
    )
    source = source.select_from(
        r.outerjoin(pl, (pl.c.project_id == r.c.project_id) & (pl.c.plan_id == r.c.plan_id))
    ).where(r.c.id == parked_id)
    return insert(r).from_select([column.name for column in source.selected_columns], source)


async def resume_parked(
    conn: AsyncConnection, user: Principal | None, parked_id: int, why: str, *, woken_by: str | None = None
) -> int:
    """Queue the run that resumes parked run ``parked_id``, whose row the caller holds locked (for a plan run, under
    the plan's lock), and end the parked one done; the new run's id. ``why`` ends the new run's first event, such as
    "as owner answered decision #3". The parked run's decisions still open go to the new run, and for an author run,
    the messages of its inbox the worker has not handed to an agent too: the answers its owner gave while it was
    parked, which its agent reads in the new run's first turn. ``woken_by``, the answer or the message that resumes
    it, titles the new run of a chat run (``chat_runs.go_on_parked``); any other keeps the parked run's title.
    ``user`` is None when the hub resumes it itself, as the reaper, for a wait that woke (``run_waits``). A request that
    carries a run's token resumes none (``run_access_rules.refuse_new_run``)."""
    run_access_rules.refuse_new_run()
    token_id = None if user is None else user.token_id
    kind = (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == parked_id))).scalar_one()
    if kind == "chat":  # a chat run goes on in its thread's session: the run is made as chat_runs makes one
        from evo_agents.hub.server.chat_runs import go_on_parked  # it queues runs through the routes that import this

        return await go_on_parked(conn, parked_id, why, woken_by=woken_by, token_id=token_id)
    reserved = select(func.nextval(func.pg_get_serial_sequence("runs", "id")))
    new_id = (await conn.execute(reserved)).scalar_one()
    # The parked run leaves the active states first: a plan has one active plan run at a time.
    await move_run(
        conn,
        parked_id,
        "parked",
        "done",
        "owner" if user is not None else "reaper",
        reason=f"resumed as #{new_id}",
        token_id=token_id,
        decisions=None,
    )
    await conn.execute(_resume_run(new_id, parked_id))
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    if kind == "author":
        undelivered = (i.c.run_id == parked_id, i.c.delivered_at.is_(None))
        await conn.execute(update(i).values(run_id=new_id).where(*undelivered))
    shown = f"resumes run #{parked_id} in its session, {why}"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": parked_id})
    await notify_queued(conn, new_id)
    return new_id


async def lock_run_of(conn: AsyncConnection, project_id: int, plan_id: str | None, decision_id: int):
    """Lock the run decision ``decision_id`` belongs to, and the decision's plan for one with a plan; (run id, its
    state, its kind). A decision of a plan is answered under the plan's dispatch lock, which its run's moves to another
    run (a resume) take too; one without a plan, under its run's row lock, which a resume holds: when the decision
    moved to another run before the lock, the lock is taken again on that run."""
    if plan_id is not None:
        await lock_plan(conn, project_id, plan_id)
    d, r = tables.decisions, tables.runs
    of_decision = select(d.c.run_id).where(d.c.id == decision_id)
    run_id = (await conn.execute(of_decision)).scalar_one()
    while True:
        locked = select(r.c.state, r.c.kind).where(r.c.id == run_id).with_for_update()
        run_state, kind = (await conn.execute(locked)).one()
        now = (await conn.execute(of_decision)).scalar_one()
        if now == run_id:
            return run_id, run_state, kind
        run_id = now


async def resumes(conn: AsyncConnection, run_id: int, run_state: str, kind: str) -> bool:
    """Whether the answer just recorded resumes run ``run_id``: a parked plan run at its first answer, a parked
    author run or a parked chat run once none of its decisions is open."""
    if run_state != "parked":
        return False
    if kind not in ("author", "chat"):
        return True
    d = tables.decisions
    still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    return (await conn.execute(still_open)).scalar_one() == 0
