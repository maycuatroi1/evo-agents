"""Which queued step runs wait for another run of their plan and repo (``evo_agents.hub.overlap``).

A queued step run is passed over by every claim while a step run of the same plan and repo that a worker holds has
edited a file the step names in its what or note. The claim drops it from the query (``blocked``), and the run's view
says who it waits for (``waits``, Run.overlap_wait). It is taken once those runs end; a step that names no path, or
runs that have edited none of them, never wait.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import overlap, runs, tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server.runs.models import OverlapWait


def _step(body, key: str | None):
    steps = body.get("steps") if isinstance(body, dict) else None
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if step_key(step, index) == key:
            return step if isinstance(step, dict) else {}
    return None


async def waits(
    conn: AsyncConnection, *, run_ids: list[int] | None = None, projects: list[int] | None = None
) -> dict[int, OverlapWait]:
    """The queued step runs among ``run_ids`` (all queued ones of ``projects`` without them) that wait for a running
    one, by run id."""
    r, rev, ev = tables.runs, tables.plan_revisions, tables.run_events
    waiting = select(r.c.id, r.c.project_id, r.c.plan_id, r.c.plan_revision, r.c.step_key, r.c.repo).where(
        r.c.kind == "step", r.c.state == "queued", r.c.plan_id.is_not(None), r.c.repo.is_not(None)
    )
    if run_ids is not None:
        waiting = waiting.where(r.c.id.in_(run_ids))
    if projects is not None:
        waiting = waiting.where(r.c.project_id.in_(projects))
    queued = (await conn.execute(waiting.order_by(r.c.id))).all()
    if not queued:
        return {}
    plans = {row.plan_id for row in queued}
    holders_query = select(r.c.id, r.c.project_id, r.c.plan_id, r.c.repo, r.c.runtime).where(
        r.c.kind == "step",
        r.c.state.in_(runs.HELD_STATES),
        r.c.plan_id.in_(plans),
        r.c.project_id.in_({row.project_id for row in queued}),
    )
    holders: dict[tuple, list] = defaultdict(list)
    for row in await conn.execute(holders_query.order_by(r.c.id)):
        holders[(row.project_id, row.plan_id, row.repo)].append(row)
    if not holders:
        return {}
    held = [row.id for rows in holders.values() for row in rows]
    events: dict[int, list] = defaultdict(list)
    called = select(ev.c.run_id, ev.c.seq, ev.c.at, ev.c.kind, ev.c.body).where(
        ev.c.run_id.in_(held), ev.c.kind == "tool_call"
    )
    for row in await conn.execute(called.order_by(ev.c.run_id, ev.c.seq)):
        events[row.run_id].append({"seq": row.seq, "at": row.at, "kind": row.kind, "body": row.body})
    edited: dict[int, list[str]] = {}
    found: dict[int, OverlapWait] = {}
    for row in queued:
        mine = holders.get((row.project_id, row.plan_id, row.repo))
        if not mine:
            continue
        body = (
            await conn.execute(
                select(rev.c.body).where(
                    rev.c.project_id == row.project_id,
                    rev.c.plan_id == row.plan_id,
                    rev.c.revision == row.plan_revision,
                )
            )
        ).scalar_one_or_none()
        step = _step(body, row.step_key)
        paths = overlap.step_paths(step.get("what"), step.get("note")) if step else []
        if not paths:
            continue
        who, files = [], []
        for other in mine:
            if other.id not in edited:
                edited[other.id] = overlap.edited_paths(other.runtime, events.get(other.id, []))
            hit = overlap.overlapping(paths, edited[other.id])
            if hit:
                who.append(other.id)
                files += [file for file in hit if file not in files]
        if who:
            found[row.id] = OverlapWait(run_ids=who, paths=files[: overlap.MAX_SHOWN])
    return found


async def blocked(conn: AsyncConnection, projects: list[int]) -> list[int]:
    """The ids of the queued step runs of ``projects`` a claim passes over now."""
    return list(await waits(conn, projects=projects))
