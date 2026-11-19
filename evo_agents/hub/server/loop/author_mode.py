"""An author run of the Loop in Loop mode (plan loop-engine, criterion a6; the checkpoint of step 1, answer 6).

The Loop hands out the author run of a build in the name of the owner of worker ``loop.worker``, and no member reads
its chat turn by turn: its prompt says so (``author.build_author_prompt`` with ``loop``, which the claim sets for a run
``runs_of.item_of_run`` finds). When its worker reports a turn over (the run moves to ``waiting``), ``turn_ended`` asks
the worker to end it ``done`` as its owner's finish would (``runs.finish_requested_at``), once the run, or a run it
goes on from, put a plan on the hub and the turn left no decision of the run open; the run's log says so, and its
owner gets no notice ``author_waiting``. A turn with no decision open and no plan of its own also ends the run when a
plan on the hub covers the build (``covering``): its agent found that plan and reused it, and the rules end the build
with a deliver of it (``rules.move_build``). Any other turn that ends with no plan on the hub, or with a decision open,
is a question: the owner is told as for any author run, and the Loop's work item waits for a person
(``author_question``) until the agent goes on.
"""

from __future__ import annotations

import logging

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.loop.runs_of import earlier_runs, item_of_run

log = logging.getLogger(__name__)

BUILD = "build"  # model.BUILD


async def written_plan(conn: AsyncConnection, run_id: int) -> str | None:
    """The plan author run ``run_id``, or a run it goes on from, put on the hub; None before its first put."""
    pr = tables.plan_revisions
    chain = await earlier_runs(conn, run_id)
    return await conn.scalar(select(pr.c.plan_id).where(pr.c.run_id.in_(chain)).order_by(pr.c.revision).limit(1))


async def turn_ended(conn: AsyncConnection, run_id: int) -> bool:
    """For author run ``run_id``, which has just moved to waiting: whether the hub ends it now (see the module), in
    the caller's transaction. False for a run that is not the Loop's, or whose turn asks a question."""
    found = await item_of_run(conn, run_id)
    if found is None or found.kind != BUILD:
        return False
    d, r = tables.decisions, tables.runs
    if await conn.scalar(select(exists().where(d.c.run_id == run_id, d.c.state == "open"))):
        return False
    plan = await written_plan(conn, run_id)
    text = f"The Loop ends this author run: plan {plan} is on the hub and the turn left no question open."
    if plan is None:
        from evo_agents.hub.server.loop.covering import covering

        cover = (await covering(conn, found.project_id, [found.id])).get(found.id)
        if cover is None:
            return False
        plan = cover.plan_id
        text = (
            f"The Loop ends this author run: plan {plan} on the hub names every use case of its build, the run wrote "
            "no plan of its own and the turn left no question open; the build goes on with a deliver of that plan."
        )
    asked = update(r).values(finish_requested_at=func.now()).where(r.c.id == run_id, r.c.finish_requested_at.is_(None))
    if (await conn.execute(asked.returning(r.c.id))).first() is not None:
        from evo_agents.hub.server.run_state import write_event  # it reads runs through the routes that import this

        await write_event(conn, run_id, {"text": text, "loop_item": found.id})
        log.info("loop author run finished", extra={"run_id": run_id, "item_id": found.id, "plan_id": plan})
    return True


__all__ = ["turn_ended", "written_plan"]
