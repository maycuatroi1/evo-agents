"""Resuming a parked run: the run that resumes it, pinned to its worker in its session, as its owner answers one of its
decisions (``answers``), a wait of its agent wakes (``run_waits``) or a message reaches a parked chat run
(``run_events``); and the locks an answer takes, which a resume takes too (``lock_run_of``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import run_access_rules
from evo_agents.hub.server.decisions import repository
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.service.views import lock_plan
from evo_agents.hub.server.security import Principal


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
    kind = await repository.run_kind(conn, parked_id)
    if kind == "chat":  # a chat run goes on in its thread's session: the run is made as chat_runs makes one
        from evo_agents.hub.server.chat_runs import go_on_parked  # it queues runs through the routes that import this

        return await go_on_parked(conn, parked_id, why, woken_by=woken_by, token_id=token_id)
    new_id = await repository.reserve_run_id(conn)
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
    await repository.insert_resume(conn, new_id, parked_id)
    await repository.move_open(conn, parked_id, new_id, inbox=kind == "author")
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
    run_id = await repository.run_of_decision(conn, decision_id)
    while True:
        run_state, kind = await repository.lock_run(conn, run_id)
        now = await repository.run_of_decision(conn, decision_id)
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
    return await repository.open_count(conn, run_id) == 0
