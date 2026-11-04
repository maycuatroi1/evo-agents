"""Runs that lose their worker, and the reaper's passes over the runs.

``release_runs`` is what happens to the runs of a revoked worker, and ``recover_runs`` (the job hub.recover_runs,
every minute) to the held runs that ran past their timeout (their agent time, ``run_seconds`` and the time since it was
last settled, over ``timeout_s``: waiting for an answer and being parked do not count), to the runs whose lease ran
out, to the runs that waited too long for an answer and to those parked too long. A run past its timeout fails, or is
cancelled when its cancel was asked for, and is not tried again. Of the runs whose lease ran out, one whose cancel was
asked for is cancelled, the last attempt fails, and any other run is lost, with its next attempt queued for the same
step (``parent_run_id`` pointing back, the attempt one higher, the runtime the dispatch asked for) and the channel
RUNS_CHANNEL notified with the new run's id. A run ``waiting`` for DECISION_WAIT is ``parked`` (cancelled instead
when its cancel was asked for), and one whose open decisions are all waits for a time or an event of the hub that
wake later than that at the first pass, so no wait holds a worker: the worker's next heartbeat says park, and the
slot is free; an author run waiting for its owner's reply in its chat is parked the same way. A run ``parked`` for
PARKED_FOR is cancelled and its open decisions expire. A revoked worker also fails the runs pinned to it, held or
queued, since no other worker may claim them, and cancels the runs parked on it, since only it has their session; an
expired lease does not, since the worker may come back. ``prune_run_events`` (hub.prune_run_events, daily)
deletes the events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago, and the sealed GitHub tokens past their end.
A move to an end state writes the run's tool figures (``tool_stats.record``), which stay after its events go, and
one of the reaper's to failed, lost or cancelled gives a run its worker left without a usage the usage its
``usage_update`` events add up to (``reaped_usage``).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.server.run_retry import next_attempt
from evo_agents.hub.server.run_state import move_run, notify_queued, past_timeout

if TYPE_CHECKING:
    from evo_agents.hub.server.github_app import GitHubApp
    from evo_agents.hub.server.sealing import Sealer

log = logging.getLogger("evo_agents.hub.server.run_state")  # the logger of the module it was split from

RECOVER_BATCH = 500  # expired runs one pass of the reaper takes
DECISION_WAIT = timedelta(seconds=runs.DECISION_WAIT_SECONDS)  # a run waiting this long for an answer is parked
PARKED_FOR = timedelta(days=runs.PARKED_DAYS)  # a run parked this long is cancelled


async def end_held(
    conn: AsyncConnection,
    run_id: int,
    state: str,
    *,
    attempt: int,
    max_attempts: int,
    cancel: bool,
    pinned_here: bool,
    reason: str,
) -> str:
    """End a held run whose worker will not extend its lease again, as the reaper, in the caller's transaction:
    cancelled when its cancel was asked for, failed when it is pinned to that worker (``pinned_here``) or on its last
    attempt, and otherwise lost with the next attempt queued, which the worker that lost the run claims only once it
    is steady (``evo_agents.hub.server.runs.service.views.steady``). Returns the state it ended in."""
    if cancel:
        await move_run(conn, run_id, state, "cancelled", "reaper", reason=reason)
        return "cancelled"
    if pinned_here or attempt >= max_attempts:
        why = "the run was pinned to it" if pinned_here else f"it was attempt {attempt} of {max_attempts}"
        await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=f"{reason}, and {why}")
        return "failed"
    await move_run(conn, run_id, state, "lost", "reaper", reason=reason, error=reason)
    next_id = (await conn.execute(next_attempt(run_id))).scalar_one()
    await notify_queued(conn, next_id)
    return "lost"


async def release_runs(conn: AsyncConnection, worker_id: int, reason: str) -> int:
    """What the reaper does to the runs of a worker that will never extend a lease again, in the caller's
    transaction: each run it holds is cancelled when its cancel was asked for, fails when it is pinned to this worker
    or on its last attempt, and is otherwise lost, with the next attempt queued for the same step. Queued runs pinned
    to the worker fail, since no other worker may claim them, and runs parked on it are cancelled, since no other
    worker has their session. Returns how many runs were moved."""
    r = tables.runs
    moved = 0
    held = (
        select(
            r.c.id,
            r.c.state,
            r.c.attempt,
            r.c.max_attempts,
            r.c.pinned_worker_id,
            r.c.cancel_requested_at.is_not(None).label("cancel"),
        )
        .where(r.c.worker_id == worker_id, r.c.state.in_(runs.HELD_STATES))
        .order_by(r.c.id)
        .with_for_update()
    )
    for row in (await conn.execute(held)).all():
        await end_held(
            conn,
            row.id,
            row.state,
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            cancel=row.cancel,
            pinned_here=row.pinned_worker_id == worker_id,
            reason=reason,
        )
        moved += 1
    pinned_queued = (
        select(r.c.id, r.c.state)
        .where(r.c.pinned_worker_id == worker_id, r.c.state == "queued")
        .order_by(r.c.id)
        .with_for_update()
    )
    for row in (await conn.execute(pinned_queued)).all():
        error = f"{reason}, and the run was pinned to it"
        await move_run(conn, row.id, row.state, "failed", "reaper", reason=reason, error=error)
        moved += 1
    parked_on = (
        select(r.c.id).where(r.c.worker_id == worker_id, r.c.state == "parked").order_by(r.c.id).with_for_update()
    )
    for run_id in (await conn.execute(parked_on)).scalars().all():
        why = f"{reason}, and only it has the session of the parked run"
        await move_run(conn, run_id, "parked", "cancelled", "reaper", reason=why)
        moved += 1
    return moved


async def end_timed_out(engine: AsyncEngine, batch: int, ended: Counter) -> None:
    """Fail every held run whose agent time is past its timeout, or cancel it when its cancel was asked for, each in a
    transaction of its own; count them in ``ended``."""
    r = tables.runs
    late_held = (r.c.state.in_(runs.HELD_STATES), r.c.kind != "local", past_timeout())
    async with engine.begin() as conn:
        late = (await conn.execute(select(r.c.id).where(*late_held).order_by(r.c.id).limit(batch))).scalars().all()
    for run_id in late:
        async with engine.begin() as conn:
            lock = (
                select(r.c.state, r.c.timeout_s, r.c.cancel_requested_at.is_not(None).label("cancel"))
                .where(r.c.id == run_id, *late_held)
                .with_for_update(skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # it ended meanwhile, or another pass took it
                continue
            state, timeout_s, cancel = row.state, row.timeout_s, row.cancel
            reason = f"it ran past its timeout of {timeout_s // 60} minutes"
            new = "cancelled" if cancel else "failed"
            await move_run(conn, run_id, state, new, "reaper", reason=reason, error=None if cancel else reason)
            ended[new] += 1


async def park_waiting(engine: AsyncEngine, batch: int, wait: timedelta, ended: Counter) -> None:
    """Park every run that has waited ``wait`` for its owner's answer, or whose open decisions are all waits for a
    time or an event of the hub none of which wakes within ``wait`` (nobody answers those, so it parks at once), or
    cancel it when its cancel was asked for, each in a transaction of its own; count them in ``ended``."""
    r, d = tables.runs, tables.decisions
    open_of_run = (d.c.run_id == r.c.id, d.c.state == "open")
    is_wait = d.c.category == runs.WAIT_DECISION_CATEGORY
    waits = exists().where(*open_of_run, is_wait)
    asks = exists().where(*open_of_run, ~is_wait)
    wakes_soon = exists().where(*open_of_run, is_wait, d.c.wake_at <= func.now() + wait)
    only_waits = and_(r.c.kind != "author", waits, ~asks, ~wakes_soon)  # an author run waits for a reply in its chat
    waited_long = (
        r.c.state == "waiting",
        r.c.kind != "local",
        or_(r.c.waiting_since < func.now() - wait, only_waits),
    )
    async with engine.begin() as conn:
        query = select(r.c.id).where(*waited_long).order_by(r.c.waiting_since).limit(batch)
        waited = (await conn.execute(query)).scalars().all()
    for run_id in waited:
        async with engine.begin() as conn:
            lock = (
                select(r.c.cancel_requested_at.is_not(None).label("cancel"), r.c.kind, only_waits.label("waits"))
                .where(r.c.id == run_id, *waited_long)
                .with_for_update(of=r, skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # answered, ended or taken by another pass meanwhile
                continue
            if row.kind == "author":  # it waits for a reply in its chat, not for an answer to a decision
                reason = f"nobody replied in its chat within {_hours(wait)}"
            elif row.waits:  # it waits for a time or an event of the hub (``run_waits``), which resumes it
                reason = "it waits for a time or an event of the hub, which resumes it"
            else:
                reason = f"nobody answered its decision within {_hours(wait)}"
            new = "cancelled" if row.cancel else "parked"
            await move_run(conn, run_id, "waiting", new, "reaper", reason=reason)
            ended[new] += 1


async def expire_parked(engine: AsyncEngine, batch: int, parked_for: timedelta, ended: Counter) -> None:
    """Cancel every run parked for ``parked_for``, its open decisions expired, each in a transaction of its own;
    count them in ``ended``. A run with an open wait whose time has not come stays: the wait wakes it
    (``run_waits``)."""
    r, d = tables.runs, tables.decisions
    waits = exists().where(
        d.c.run_id == r.c.id,
        d.c.state == "open",
        d.c.category == runs.WAIT_DECISION_CATEGORY,
        d.c.wake_at > func.now(),
    )
    parked_long = (r.c.state == "parked", r.c.parked_at < func.now() - parked_for, ~waits)
    async with engine.begin() as conn:
        query = select(r.c.id).where(*parked_long).order_by(r.c.parked_at).limit(batch)
        parked = (await conn.execute(query)).scalars().all()
    for run_id in parked:
        async with engine.begin() as conn:
            lock = select(r.c.id).where(r.c.id == run_id, *parked_long).with_for_update(skip_locked=True)
            if (await conn.execute(lock)).one_or_none() is None:
                continue
            reason = f"it stayed parked for {_hours(parked_for)} without an answer"
            await move_run(conn, run_id, "parked", "cancelled", "reaper", reason=reason, decisions="expired")
            ended["cancelled"] += 1


def _hours(span: timedelta) -> str:
    """``span`` in words: whole hours up to two days, then whole days, else seconds."""
    seconds = int(span.total_seconds())
    if seconds % 60 == 0 and seconds < 3600:
        return f"{seconds // 60} minute{'s' if seconds != 60 else ''}"
    if seconds % 3600 == 0 and seconds <= 2 * 86400:
        return f"{seconds // 3600} hour{'s' if seconds != 3600 else ''}"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} days"
    return f"{seconds} seconds"


async def expire_seats(engine: AsyncEngine, sealer, github_app) -> dict:
    """The job hub.expire_seats (``org_expiry.expire_seats``): the org seats past their end date ended, and the leases
    of the people whose primary seat ended taken back, and the leads of an intern's unit told of a decision of the
    intern's runs open 24 hours (``principals.escalate``). The hub's worker reaches it through this module."""
    from evo_agents.hub.server import org_expiry, principals

    return {**await org_expiry.expire_seats(engine, sealer, github_app), "escalated": await principals.escalate(engine)}


async def reap_pool_nodes(engine: AsyncEngine, sealer, github_app) -> dict:
    """The job hub.reap_pool_nodes (``worker_reaper.reap_pool_nodes``): each pool node offline longer than its pool's
    ``reap_after_days`` revoked, as its owner's revoke would. The hub's worker reaches it through this module."""
    from evo_agents.hub.server import worker_reaper

    return await worker_reaper.reap_pool_nodes(engine, sealer, github_app)


async def recover_runs(
    engine: AsyncEngine,
    batch: int = RECOVER_BATCH,
    *,
    decision_wait: timedelta = DECISION_WAIT,
    parked_for: timedelta = PARKED_FOR,
    sealer: Sealer | None = None,
    github_app: GitHubApp | None = None,
) -> dict:
    """One pass of the reaper: every held run past its timeout ends as ``end_timed_out`` says, then every held run
    whose lease ran out ends as ``end_held`` says, the waits whose time or event came wake their runs
    (``run_waits.wake_waits``), a run that waited ``decision_wait`` for an answer, or waits only for a time or an
    event of the hub, is parked and one parked for
    ``parked_for`` cancelled, each in a transaction of its own, a question run queued for
    ``question_runs.QUEUE_SECONDS`` fails (no_worker), a run running or verifying with no event for
    ``triage.STALE_MINUTES`` while its worker heartbeats opens its triage thread (``triage.open_stale``), and a queued
    run pinned to a revoked worker fails. Last, the GitHub tokens of the leases given back, by these runs or earlier, are revoked at GitHub with
    ``github_app`` (``credentials.revoke_tokens``; nothing without the App or ``sealer``). Returns how many runs
    ended in each state, and how many were parked."""
    r, w = tables.runs, tables.workers
    ended: Counter[str] = Counter()
    await end_timed_out(engine, batch, ended)
    lapsed = (r.c.state.in_(runs.HELD_STATES), r.c.kind != "local", r.c.lease_expires_at < func.now())
    async with engine.begin() as conn:
        query = select(r.c.id).where(*lapsed).order_by(r.c.lease_expires_at).limit(batch)
        expired = (await conn.execute(query)).scalars().all()
    for run_id in expired:
        async with engine.begin() as conn:
            lock = (
                select(
                    r.c.state,
                    r.c.attempt,
                    r.c.max_attempts,
                    r.c.cancel_requested_at.is_not(None).label("cancel"),
                    w.c.name.label("worker"),
                )
                .select_from(r.outerjoin(w, w.c.id == r.c.worker_id))
                .where(r.c.id == run_id, *lapsed)
                .with_for_update(of=r, skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # a heartbeat extended it, or another pass took it
                continue
            reason = f"its worker {row.worker} stopped extending the lease"
            ended[
                await end_held(
                    conn,
                    run_id,
                    row.state,
                    attempt=row.attempt,
                    max_attempts=row.max_attempts,
                    cancel=row.cancel,
                    pinned_here=False,
                    reason=reason,
                )
            ] += 1
    from evo_agents.hub.server import run_waits  # it moves runs through this module

    await run_waits.wake_waits(engine, batch)  # first, so a wait that came wakes before its run is parked or expires
    await park_waiting(engine, batch, decision_wait, ended)
    await run_waits.resume_answered(engine, batch)  # a run parked in the pass its wait woke, the answer untaken
    await expire_parked(engine, batch, parked_for, ended)
    from evo_agents.hub.server import local_runs  # it moves runs through this module

    await local_runs.end_silent(engine, batch)  # a local run no worker holds: it is neither lost nor parked
    from evo_agents.hub.server import question_dispatch  # it moves runs through this module

    await question_dispatch.expire_queued(engine, batch, ended)  # a question run no worker claimed in time
    from evo_agents.hub.server import triage  # it reads runs through this module

    await triage.open_stale(engine)  # a run that says nothing while its worker heartbeats gets its triage thread
    async with engine.begin() as conn:
        unclaimable = (
            select(r.c.id, w.c.name)
            .join_from(r, w, w.c.id == r.c.pinned_worker_id)
            .where(r.c.state == "queued", w.c.revoked_at.is_not(None))
            .order_by(r.c.id)
            .limit(batch)
            .with_for_update(of=r, skip_locked=True)
        )
        for run_id, worker in (await conn.execute(unclaimable)).all():
            reason = f"its pinned worker {worker} was revoked"
            error = f"{reason}, so no worker may claim it"
            await move_run(conn, run_id, "queued", "failed", "reaper", reason=reason, error=error)
            ended["failed"] += 1
    report = {state: ended.get(state, 0) for state in ("lost", "failed", "cancelled", "parked")}
    if expired or ended:
        log.warning("runs recovered", extra=report)
    from evo_agents.hub.server import credentials, share_leases

    async with engine.begin() as conn:  # the leases still out under a share that was revoked or ended
        await share_leases.end_lapsed(conn)
    await credentials.revoke_tokens(engine, sealer, github_app)
    return report


async def prune_run_events(engine: AsyncEngine, days: int, store=None) -> dict:
    """Delete the events of runs that ended more than ``days`` days ago, once their tool figures are written from them
    (``tool_stats.record``, for a run that ended before the hub wrote them at the end), and the outputs of source tool
    of those runs that nothing carries, with the blobs no output names any longer (``output_prune``, whose objects go
    from ``store`` now when it is given), and the files of the messages to those runs (``attachment_prune``); and drop
    the sealed values of the GitHub tokens leased to runs that are past their end (``credentials.drop_expired``)."""
    from evo_agents.hub import attachment_prune, output_prune
    from evo_agents.hub.server import credentials, tool_stats

    r, e = tables.runs, tables.run_events
    old = [r.c.finished_at.is_not(None), r.c.finished_at < func.now() - timedelta(days=days)]
    logged = select(r.c.id).where(*old, exists().where(e.c.run_id == r.c.id)).order_by(r.c.id)
    prune = delete(e).where(e.c.run_id == r.c.id, *old)
    async with engine.begin() as conn:
        counted = (await conn.execute(logged)).scalars().all()
        for run_id in counted:
            await tool_stats.record(conn, run_id)
        deleted = (await conn.execute(prune)).rowcount
        dropped = await credentials.drop_expired(conn)
    log.info(
        "run events pruned",
        extra={"deleted": deleted, "runs": len(counted), "days": days, "tokens_dropped": dropped},
    )
    pruned = await output_prune.prune_tool_outputs(engine, store, days)
    pruned |= await attachment_prune.prune_run_files(engine, store, days)  # the files of the runs' messages
    return {"deleted": deleted, "days": days, "tokens_dropped": dropped, **pruned}
