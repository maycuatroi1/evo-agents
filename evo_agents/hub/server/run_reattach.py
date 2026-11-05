"""A lost run whose worker comes back for it.

A worker whose host stalls past the lease (every daemon of the machine silent at once: a host short of CPU or memory)
loses its runs to the reaper (``run_reaper``) while their agents go on working, and the hub queues their next attempts.
When the worker's heartbeat named such a run again, the hub answered cancel: the worker stopped a live agent, and the
next attempt began the work again (runs #1620 and #1629 came back 57 s and 11 s after the reaper took them).
``reattach_lost`` re-attaches the run instead, in the heartbeat's transaction, when

- the worker's daemon says it re-attaches (``runs.REATTACH``) and does not name the run among the orphans a previous
  daemon left (``orphans`` of the heartbeat), whose agent it stops whatever the answer;
- the run is this worker's, lost less than ``runs.REATTACH_SECONDS`` ago, its cancel not asked, and of a kind whose loss
  moved nothing on but its next attempt (REATTACH_KINDS: the loss of a run of the Curator moved its night, its change,
  its replay or its probe on already);
- its next attempt still waits in the queue, which no worker claimed.

The reaper cancels the next attempt without writing the plan (a lost run left its step as it was), and moves the run back
to the held state it was lost in (``run_state.move_run`` with ``reattach``), its lease running from now and the stall not
counted in its agent time. The decisions its loss cancelled are open again (those its agent asked; a gate's that was
cancelled earlier stays so), and the browser identities it was dispatched with that are still free are its own again.
Its loss gave back its leases and the hub revoked the GitHub tokens among them at GitHub, so the heartbeat's answer says
``renew``, and the worker takes the run's leases again.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import browser_holds
from evo_agents.hub.server.run_state import move_run

REATTACH = timedelta(seconds=runs.REATTACH_SECONDS)
# the kinds whose loss did nothing but queue the next attempt (``run_state.record_move``)
REATTACH_KINDS = ("step", "plan", "author", "chat", "task", "release", "history")


def _lost_here(worker_id: int, run_ids: list[int]):
    """The runs of ``run_ids`` worker ``worker_id`` lost within REATTACH, of REATTACH_KINDS and with no cancel asked,
    each with the id of its next attempt still queued, both rows locked; one a claim holds is left out."""
    r, n = tables.runs, tables.runs.alias("n")
    return (
        select(r.c.id, n.c.id.label("next_id"))
        .join_from(r, n, n.c.parent_run_id == r.c.id)
        .where(
            r.c.worker_id == worker_id,
            r.c.id.in_(run_ids),
            r.c.state == "lost",
            r.c.kind.in_(REATTACH_KINDS),
            r.c.cancel_requested_at.is_(None),
            r.c.finished_at > func.now() - REATTACH,
            n.c.state == "queued",
        )
        .order_by(r.c.id)
        .with_for_update(of=[r, n], skip_locked=True)
    )


def _lost_from(run_id: int):
    """The state run ``run_id`` was in when it was lost, as the event of that move says."""
    e = tables.run_events
    return (
        select(e.c.body["from"].astext)
        .where(e.c.run_id == run_id, e.c.kind == "state", e.c.body["to"].astext == "lost")
        .order_by(e.c.seq.desc())
        .limit(1)
    )


async def reattach_lost(
    conn: AsyncConnection, worker_id: int, worker: str, run_ids: list[int], lease: timedelta
) -> set[int]:
    """Re-attach to worker ``worker_id``, named ``worker``, each run of ``run_ids`` it lost a short while ago whose next
    attempt waits unclaimed (see the module's docstring), its lease ``lease`` from now, in the caller's transaction;
    the ids of the runs re-attached."""
    back: set[int] = set()
    if not run_ids:
        return back
    reason = f"its worker {worker} came back within {runs.REATTACH_SECONDS // 60} minutes"
    d = tables.decisions
    for run_id, next_id in (await conn.execute(_lost_here(worker_id, run_ids))).all():
        held = (await conn.execute(_lost_from(run_id))).scalar_one_or_none()
        if held not in runs.HELD_STATES:
            continue
        await move_run(
            conn, next_id, "queued", "cancelled", "reaper", reason=f"run #{run_id} went on: {reason}", recorded=False
        )
        await move_run(conn, run_id, "lost", held, "reaper", reason=reason, lease=lease, reattach=True)
        asked = (d.c.run_id == run_id, d.c.state == "cancelled", d.c.answered_at.is_(None))
        await conn.execute(
            update(d).values(state="open").where(*asked, d.c.category.in_(runs.ASKED_DECISION_CATEGORIES))
        )
        await browser_holds.take(conn, run_id)
        back.add(run_id)
    return back
