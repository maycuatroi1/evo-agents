"""Which work item of the Loop a run was handed out for (plan loop-engine, criterion a6), for the code that treats the
Loop's runs apart from a member's: the prompt of an author run in Loop mode (``runs.service.claims``), its end when a
turn leaves the plan on the hub and asks nothing (``author_mode``), and the version @ba writes in a use_case task run
(the core plugin srs's ``srs__propose``).

A run is the Loop's when a ``dispatched`` row of a work item's ledger names it, or names a run it goes on from: one it
resumes (``resume_of_run_id``) or retries (``parent_run_id``), at most MAX_HOPS back. Only the hub's tables are read:
nothing here imports the rest of the Loop, so a plugin and the run service may import it without a cycle.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

DISPATCHED = "dispatched"  # model.DISPATCHED: the ledger row of a run handed out
MAX_HOPS = 20  # view.MAX_HOPS


async def earlier_runs(conn: AsyncConnection, run_id: int) -> list[int]:
    """Run ``run_id`` and the runs it goes on from, newest first, at most MAX_HOPS of them."""
    r = tables.runs
    found: list[int] = []
    current: int | None = run_id
    while current is not None and current not in found and len(found) < MAX_HOPS:
        found.append(current)
        back = (await conn.execute(select(r.c.resume_of_run_id, r.c.parent_run_id).where(r.c.id == current))).first()
        current = None if back is None else (back.resume_of_run_id or back.parent_run_id)
    return found


async def item_of_run(conn: AsyncConnection, run_id: int):
    """The work item run ``run_id`` was handed out for, or that a run it goes on from was (see the module): its id,
    kind, project, goal and state; None for a run that is not the Loop's."""
    i, e = tables.loop_items, tables.loop_item_events
    chain = await earlier_runs(conn, run_id)
    query = (
        select(i.c.id, i.c.kind, i.c.project_id, i.c.goal_id, i.c.state)
        .join_from(e, i, i.c.id == e.c.item_id)
        .where(e.c.run_id.in_(chain), e.c.kind == DISPATCHED)
        .order_by(e.c.run_id.desc(), e.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def goal_owner(conn: AsyncConnection, goal_id: int | None) -> int | None:
    """The owner of goal ``goal_id`` at its newest revision; None without one."""
    if goal_id is None:
        return None
    g, r = tables.goals, tables.goal_revisions
    query = select(r.c.owner_id).join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
    return await conn.scalar(query.where(g.c.id == goal_id))


async def use_case_author(conn: AsyncConnection, run_id: int) -> int | None:
    """The member a version @ba proposes in task run ``run_id`` is written as, when the Loop handed the run out for a
    use_case work item: the owner of its goal (the checkpoint of step 1, answer 4). None for any other run."""
    found = await item_of_run(conn, run_id)
    if found is None or found.kind != "use_case":
        return None
    return await goal_owner(conn, found.goal_id)


__all__ = ["earlier_runs", "goal_owner", "item_of_run", "use_case_author"]
