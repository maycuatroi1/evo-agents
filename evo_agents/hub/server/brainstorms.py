"""The Loop's brainstorm runs of a goal, read from the tables alone. Here, not in the canvas feature, because the goals
read them (``stale_for_goal``) and a feature may be imported by no other (lint-imports, contract hub-feature-canvas);
``server.canvas.proposals`` re-exports both."""

from __future__ import annotations

from sqlalchemy import select, union, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

BRAINSTORM = "brainstorm"  # loop.model.KINDS: the Loop's work item whose task run brainstorms a goal
DISPATCHED = "dispatched"  # loop.model: the event of the ledger that hands a work item its run


def brainstorm_runs(project_id: int | None = None):
    """Each run the Loop handed out for its brainstorm of a goal (of project ``project_id`` alone when given), with the
    goal (``run_id``, ``goal_id``): from the ledger of its work items, as the Loop's view counts a goal's open proposals
    (``loop.view``), and from the work item itself for the run it holds now."""
    i, e = tables.loop_items, tables.loop_item_events
    brainstorm = (i.c.kind == BRAINSTORM) & i.c.goal_id.is_not(None)
    if project_id is not None:
        brainstorm &= i.c.project_id == project_id
    ledger = select(e.c.run_id, i.c.goal_id).join_from(e, i, i.c.id == e.c.item_id)
    ledger = ledger.where(brainstorm, e.c.kind == DISPATCHED, e.c.run_id.is_not(None))
    held = select(i.c.run_id, i.c.goal_id).where(brainstorm, i.c.run_id.is_not(None))
    return union(ledger, held).subquery("brainstorm_runs")


async def stale_for_goal(conn: AsyncConnection, goal_id: int) -> int:
    """The open proposals the runs of the Loop's brainstorms of goal ``goal_id`` made go stale, as a change of their
    claim makes them (no one decided them, so they name no decider and no reason): the statement or the metric they
    were brainstormed for changed. How many went stale."""
    p, runs = tables.canvas.proposals, brainstorm_runs()
    of_goal = select(runs.c.run_id).where(runs.c.goal_id == goal_id)
    done = await conn.execute(update(p).where(p.c.status == "open", p.c.run_id.in_(of_goal)).values(status="stale"))
    return done.rowcount


__all__ = ["BRAINSTORM", "DISPATCHED", "brainstorm_runs", "stale_for_goal"]
