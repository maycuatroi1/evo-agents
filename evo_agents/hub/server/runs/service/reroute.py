"""A run whose worker lacks a program goes to the next worker, before anything else is tried.

The hub cannot tell which programs a worker has, so a run nobody pinned may be claimed by one that lacks a program its
verify calls, while another worker of its owner has it. The preflight of that worker says so before the run's agent
starts (the run is still ``leased``, ``triage_rules.lacks_program``). Then the run does not fail: it ends ``lost``, as a
run that lost its worker does, and its next attempt (``run_retry.next_attempt``) is pinned to the best fit worker it was
not tried on (``candidates.candidates_of``, the owner's own before a shared one). Nothing is sent to its owner and no
triage thread opens. So the run's ``max_attempts`` bounds how many workers are tried.

The run fails as it always did, and its triage thread opens, when a person pinned it to that worker (the first attempt
says so: the pins of the later ones are the hub's), on its last attempt, or when no fit worker is left. The agent of that
thread then installs the program on a worker the run was tried on (``triage._install_worker``) and runs it again.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, triage_rules
from evo_agents.hub.server.run_retry import next_attempt
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.service.candidates import WorkerCandidate, candidates_of

log = logging.getLogger(__name__)


async def tried_on(conn: AsyncConnection, run_id: int) -> tuple[list[int], bool]:
    """(the workers run ``run_id`` and the attempts this module moved it from were on, the latest first; whether a
    person pinned the first of them). A run this module did not move is one attempt alone."""
    r = tables.runs
    workers: list[int] = []
    current = run_id
    while True:
        query = select(r.c.worker_id, r.c.pinned_worker_id, r.c.parent_run_id).where(r.c.id == current)
        row = (await conn.execute(query)).one()
        if row.worker_id is not None:
            workers.append(row.worker_id)
        if row.parent_run_id is None:
            return workers, row.pinned_worker_id is not None
        query = select(r.c.state, r.c.failure_cause).where(r.c.id == row.parent_run_id)
        parent = (await conn.execute(query)).one()
        if parent.state != "lost" or parent.failure_cause != triage_rules.MISSING_TOOL:
            return workers, row.pinned_worker_id is not None
        current = row.parent_run_id


async def next_worker(
    conn: AsyncConnection, run_id: int, state: str, cause: str | None, error: str | None
) -> WorkerCandidate | None:
    """The worker run ``run_id``, ``state`` now, goes to instead of failing with ``cause`` and ``error``; None when it
    fails (see the module's docstring). The caller holds the run's row locked."""
    if state != "leased" or not triage_rules.lacks_program(cause, error):
        return None
    r, p, g = tables.runs, tables.projects, tables.grants
    row = (
        await conn.execute(
            select(
                r.c.kind, r.c.attempt, r.c.max_attempts, r.c.project_id, r.c.dispatched_by, r.c.cancel_requested_at
            ).where(r.c.id == run_id)
        )
    ).one()
    if row.kind not in triage_rules.KINDS or row.project_id is None:
        return None
    if row.attempt >= row.max_attempts or row.cancel_requested_at is not None:
        return None
    tried, pinned = await tried_on(conn, run_id)
    if pinned:
        return None
    project = (await conn.execute(select(p.c.name).where(p.c.id == row.project_id))).scalar_one()
    held = select(g.c.role).where(g.c.user_id == row.dispatched_by, g.c.project_id == row.project_id)
    role = (await conn.execute(held)).scalar_one_or_none()
    found = await candidates_of(
        conn,
        project=project,
        project_id=row.project_id,
        role=role or "reader",
        owner_id=row.dispatched_by,
        kind=row.kind,
        state=state,
        run_id=run_id,
    )
    return next((worker for worker in found.workers if worker.fit and worker.worker_id not in tried), None)


async def move_on(
    conn: AsyncConnection,
    run_id: int,
    state: str,
    worker: WorkerCandidate,
    *,
    error: str,
    columns: dict,
    token_id: int | None,
) -> int:
    """End run ``run_id`` ``lost`` and queue its next attempt, pinned to ``worker``; the id of that attempt."""
    reason = f"{error}; the hub moved the run to worker {worker.worker}"
    await move_run(
        conn, run_id, state, "lost", "reaper", reason=reason, error=error, columns=columns, token_id=token_id
    )
    next_id = (await conn.execute(next_attempt(run_id))).scalar_one()
    r = tables.runs
    await conn.execute(update(r).values(pinned_worker_id=worker.worker_id).where(r.c.id == next_id))
    text = f"goes on from run #{run_id} on worker {worker.worker}: the worker of that run lacks a program it calls"
    await write_event(conn, next_id, {"text": text, "moved_from_run_id": run_id})
    await notify_queued(conn, next_id)
    log.info("run moved to another worker", extra={"run_id": run_id, "next": next_id, "worker": worker.worker})
    return next_id
