"""Tier 0 of the queue of operations events (plan hub-steward, criterion a6): what the hub does by itself, with no
model, for a project whose default system turns ops on, in the pass of the queue (``ops_queue.ops_pass``) after it
took the project's events.

Redelivery. A chat or task run that failed because of its machine, not its work (``CAUSES``: its checkout, its worker
stopped, its lease lost, the network, a 5xx of the hub, its account's login), is handed again once, as a person did
by hand before: a chat run's mention is said again, by the hub, in its thread (``chat_runs.hand_mentions``); a task
run gets its next attempt for its plugin (``task_dispatch.hand_again``), the same task. It is handed when its worker
is healthy again, a heartbeat after the failure saying enough free disk (``ops_events.disk_band``) and the login of
its runtime ok, or when another online worker of the same member and pool that serves the project is. Its item
then leaves its batch (``ops_queue.settle``), handled by ``REDELIVER``. Until then the item waits in its batch; once
the batch closed, it is the Orchestrator's.

A run is handed again only once: none whose work the hub handed again before (the run it goes on from, through its
parent, the session it resumes, or the hub's message that started it), so a second failure goes to the Orchestrator.
A run of a step, a plan or an author keeps its triage. A run an Orchestrator took in a thread of rule failed is not
handed: its item leaves the batch, handled by ``ORCHESTRATOR``, when that thread has an outcome or its run ended
done, and waits while that run goes on.

Notices. An unread notice of the project whose work is done is marked read: ``run_failed`` of a run done again since
(a retry, a resumed session, or a later run of the same step or plan) or of a plan now completed, and
``curator_paused`` once no schedule of the project is paused and a run of the night shift ended done after it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from evo_agents.hub import hub_events, ops_events, ops_queue, runs, tables, triage_rules
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_runs import Hand, chat_run, hand_mentions
from evo_agents.hub.server.chat_store import insert_mentions, insert_message
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.orchestrator import origin_of
from evo_agents.hub.server.profile_store import by_id

log = logging.getLogger(__name__)

CAUSES = ("checkout", "worker_stopped", "lease_lost", "network", "hub_5xx", "account_auth")
KINDS = ("chat", "task")  # a step, plan or author run keeps its triage
REDELIVER = "tier0:redeliver"  # ops_items.handled_by of an item whose run the hub handed again
ORCHESTRATOR = "orchestrator"  # ops_items.handled_by of an item whose run an Orchestrator took
MAX_CHAIN = 50  # runs the check of a run's earlier redelivery walks back


@dataclass(frozen=True)
class Failed:
    """A run an item of an open batch tells failed of an environment cause."""

    run_id: int
    kind: str
    cause: str
    worker_id: int | None
    dispatched_by: int
    finished_at: datetime | None
    runtime: str | None


async def tier0(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """Run the rules of tier 0 for project ``project_id`` in the caller's transaction, nothing while its ops is off:
    how many runs it handed again, items it settled for an Orchestrator, and notices it marked read."""
    if not (await ops_queue.ops_config(conn, project_id)).enabled:
        return {}
    redelivered, taken = await redeliver(conn, project_id)
    return {"redelivered": redelivered, "orchestrated": taken, "read": await mark_read(conn, project_id)}


# Redelivery


def _failed_items(project_id: int):
    i, b, r = tables.ops_items, tables.ops_batches, tables.runs
    return (
        select(
            r.c.id,
            r.c.kind,
            r.c.failure_cause,
            r.c.worker_id,
            r.c.dispatched_by,
            r.c.finished_at,
            func.coalesce(func.nullif(r.c.runtime, "any"), r.c.requested_runtime).label("runtime"),
        )
        .select_from(i.join(b, b.c.id == i.c.batch_id).join(r, r.c.id == i.c.run_id))
        .where(
            i.c.project_id == project_id,
            i.c.state == "pending",
            i.c.kind == hub_events.RUN_ENDED,
            b.c.state == "open",
            r.c.state == "failed",
            r.c.kind.in_(KINDS),
            r.c.failure_cause.in_(CAUSES),
        )
        .order_by(i.c.id)
        .with_for_update(of=i, skip_locked=True)
    )


async def redeliver(conn: AsyncConnection, project_id: int) -> tuple[int, int]:
    """Hand again each run of a pending item of the project's open batches that failed of a cause of ``CAUSES``, as
    the module's docstring says; (how many it handed, how many an Orchestrator took)."""
    rows = (await conn.execute(_failed_items(project_id))).all()
    found = {row.id: Failed(*row) for row in rows}
    handed = taken = 0
    for failed in found.values():
        if await _handed_before(conn, failed.run_id):
            continue
        orchestrated = await _orchestrated(conn, failed.run_id)
        if orchestrated == "taken":
            taken += await ops_queue.settle(conn, project_id, by=ORCHESTRATOR, run_ids=[failed.run_id])
            continue
        worker_id = None if orchestrated == "wait" else await healthy_worker(conn, project_id, failed)
        if worker_id is None:
            continue
        new_id = await hand_again(conn, failed, worker_id)
        if new_id is not None:
            await ops_queue.settle(conn, project_id, by=REDELIVER, run_ids=[failed.run_id])
            log.info("run handed again", extra={"run_id": failed.run_id, "next": new_id, "cause": failed.cause})
            handed += 1
    return handed, taken


async def _earlier(conn: AsyncConnection, run_id: int) -> set[int]:
    """The runs run ``run_id`` goes on from: its parent, the session it resumes, the run the hub's message that
    started it names."""
    r, m = tables.runs, tables.chat_messages
    query = (
        select(r.c.parent_run_id, r.c.resume_of_run_id, m.c.run_id.label("message_run"))
        .select_from(
            r.outerjoin(
                m, and_(m.c.id == r.c.chat_message_id, m.c.author_user_id.is_(None), m.c.author_profile_id.is_(None))
            )
        )
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return set()
    return {found for found in (row.parent_run_id, row.resume_of_run_id, row.message_run) if found is not None}


async def _handed_before(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the hub handed again run ``run_id``, or a run it goes on from (``_earlier``, back MAX_CHAIN runs)."""
    seen, todo = set(), {run_id}
    while todo and len(seen) < MAX_CHAIN:
        current = todo.pop()
        seen.add(current)
        todo |= await _earlier(conn, current) - seen
    i = tables.ops_items
    query = select(exists().where(i.c.run_id.in_(list(seen)), i.c.handled_by == REDELIVER))
    return bool(await conn.scalar(query))


async def _orchestrated(conn: AsyncConnection, run_id: int) -> str | None:
    """``taken`` when an Orchestrator answered the thread of rule failed of run ``run_id`` (an outcome, or its run
    ended done), ``wait`` while its run goes on; None when no thread holds the run, or its run ended otherwise."""
    t, r = tables.orchestrator_threads, tables.runs
    query = (
        select(t.c.outcome, r.c.state)
        .select_from(t.outerjoin(r, r.c.id == t.c.orchestrator_run_id))
        .where(t.c.subject_run_id == run_id, t.c.rule == triage_rules.FAILED)
    )
    rows = (await conn.execute(query)).all()
    if any(row.outcome is not None or row.state == "done" for row in rows):
        return "taken"
    if any(row.state is not None and row.state not in runs.TERMINAL_STATES for row in rows):
        return "wait"
    return None


def healthy(health: dict | None, runtime: str | None, *, leased: bool = False) -> bool:
    """Whether a worker's stored health report says enough free disk and the login of ``runtime`` ok or leased
    (``ops_events.LOGGED_IN``; of any runtime for a run that asked for any); with ``leased`` (the run logs in with a hub
    account), enough free disk."""
    if not health or ops_events.disk_band(health) != "ok":
        return False
    if leased:
        return True
    logins = health.get("logins") or {}
    if runtime and runtime != "any":
        return logins.get(runtime) in ops_events.LOGGED_IN
    return any(state in ops_events.LOGGED_IN for state in logins.values())


async def healthy_worker(conn: AsyncConnection, project_id: int, failed: Failed) -> int | None:
    """The worker the work of ``failed`` goes to again: its own when a heartbeat after the failure says it is healthy
    (``healthy``), else another online one that is, of the same member and pool, serving the project; None when
    none is."""
    w, wp = tables.workers, tables.worker_projects
    pool = select(w.c.pool_id).where(w.c.id == failed.worker_id).scalar_subquery()
    serves = select(wp.c.worker_id).where(wp.c.project_id == project_id)
    other = and_(w.c.owner_id == failed.dispatched_by, w.c.id.in_(serves), w.c.pool_id.is_not_distinct_from(pool))
    query = select(w.c.id, w.c.health).where(
        w.c.revoked_at.is_(None),
        w.c.drained_at.is_(None),
        w.c.last_heartbeat_at >= func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS),
        or_(w.c.id == failed.worker_id, other),
    )
    if failed.finished_at is not None:
        query = query.where(w.c.last_heartbeat_at > failed.finished_at)
    leased = await ops_queue.run_leased(conn, failed.run_id, failed.runtime)
    rows = (await conn.execute(query.order_by(w.c.id))).all()
    fit = [row.id for row in rows if healthy(row.health, failed.runtime, leased=leased)]
    if failed.worker_id in fit:
        return failed.worker_id
    return fit[0] if fit else None


async def failed_run(conn: AsyncConnection, run_id: int) -> Failed | None:
    """Run ``run_id`` of the project as ``Failed`` reads it, when it is a chat or task run that ended failed or lost;
    None otherwise (the Orchestrator's ops actions, ``ops_actions``)."""
    r = tables.runs
    query = select(
        r.c.id,
        r.c.kind,
        func.coalesce(r.c.failure_cause, r.c.state),
        r.c.worker_id,
        r.c.dispatched_by,
        r.c.finished_at,
        func.coalesce(func.nullif(r.c.runtime, "any"), r.c.requested_runtime),
    ).where(r.c.id == run_id, r.c.kind.in_(KINDS), r.c.state.in_(("failed", "lost")))
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else Failed(*row)


async def hand_again(conn: AsyncConnection, failed: Failed, worker_id: int | None) -> int | None:
    """Hand the work of ``failed`` again, to ``worker_id`` (any worker that fits, for None) for a new session, in a
    savepoint undone when it hands none; the run that took it, or None."""
    savepoint = await conn.begin_nested()
    try:
        if failed.kind == "chat":
            new_id = await _chat_again(conn, failed, worker_id)
        else:
            from evo_agents.hub.server import task_dispatch  # here: first of all, its import is circular

            why = f"it failed ({failed.cause}) because of its machine, and a worker is healthy now"
            new_id, refusal = await task_dispatch.hand_again(conn, failed.run_id, why, worker_id)
            if refusal is not None:
                log.info("task not handed again", extra={"run_id": failed.run_id, "why": refusal})
    except Exception:
        await savepoint.rollback()
        log.exception("handing a run again failed", extra={"run_id": failed.run_id})
        return None
    if new_id is None:
        await savepoint.rollback()
        return None
    await savepoint.commit()
    return new_id


async def _chat_again(conn: AsyncConnection, failed: Failed, worker_id: int | None) -> int | None:
    """Say the mention of chat run ``failed`` again, as the hub, in its thread, for the person at the origin of its
    work, and hand it to its agent on ``worker_id``; the run it reached, or None when it reached none."""
    found = await chat_run(conn, failed.run_id)
    profile = None if found is None else await by_id(conn, found.profile_id)
    if found is None or profile is None or profile.deleted_at is not None:
        return None
    origin = await origin_of(conn, failed.run_id)
    try:
        chat = await chat_access(conn, origin.person, found.chat_id)
    except HTTPException:  # the person left the chat, or no longer clears its label
        return None
    body = (
        f"Run #{failed.run_id} of @{profile.handle} failed ({failed.cause}) because of its machine, not its work: "
        f"the hub hands the work again.\n\n@{profile.handle}"
    )
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=found.root_id, run_id=failed.run_id)
    kept, _, _ = await resolve_mentions(conn, chat, origin.person, [profile.handle])
    person = origin.person
    hand = Hand(
        chat,
        message_id,
        found.root_id,
        profile,
        person.user_id,
        person.login,
        origin.via,
        origin.hop,
        "the hub",
        body,
        on_worker=worker_id,
    )
    refusals = await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    run_id = next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)
    if run_id is None:
        log.info("chat run not handed again", extra={"run_id": failed.run_id, "why": "; ".join(refusals)})
    return run_id


# Notices


def _done_again():
    """A notice's run was done again since: a later run done that retries it, resumes its session, or runs the same
    step or plan."""
    n, failed, later = tables.notifications, aliased(tables.runs), aliased(tables.runs)
    same = and_(
        failed.c.plan_id.is_not(None),
        later.c.project_id == failed.c.project_id,
        later.c.plan_id == failed.c.plan_id,
        later.c.kind == failed.c.kind,
        later.c.step_key.is_not_distinct_from(failed.c.step_key),
    )
    went_on = or_(later.c.parent_run_id == failed.c.id, later.c.resume_of_run_id == failed.c.id, same)
    return exists(
        select(later.c.id)
        .select_from(failed.join(later, later.c.id > failed.c.id))
        .where(failed.c.id == n.c.run_id, later.c.state == "done", went_on)
    )


def _plan_closed():
    n, r, pl = tables.notifications, tables.runs, tables.plans
    return exists(
        select(pl.c.plan_id)
        .select_from(r.join(pl, and_(pl.c.project_id == r.c.project_id, pl.c.plan_id == r.c.plan_id)))
        .where(r.c.id == n.c.run_id, pl.c.area == "completed")
    )


def _curator_back():
    """The night shift of a notice's project runs again: no schedule of it paused, and a run of it done since."""
    n, s, r = tables.notifications, tables.schedules, tables.runs
    paused = exists(select(s.c.id).where(s.c.project_id == n.c.project_id, s.c.paused_at.is_not(None)))
    ran = exists(
        select(r.c.id).where(
            r.c.project_id == n.c.project_id,
            r.c.schedule_id.is_not(None),
            r.c.state == "done",
            r.c.finished_at > n.c.created_at,
        )
    )
    return and_(~paused, ran)


async def mark_read(conn: AsyncConnection, project_id: int) -> int:
    """Mark read the unread notices of project ``project_id`` whose work is done (the module's docstring); how many."""
    n = tables.notifications
    done = or_(
        and_(n.c.notice_kind == "run_failed", n.c.run_id.is_not(None), or_(_done_again(), _plan_closed())),
        and_(n.c.notice_kind == "curator_paused", _curator_back()),
    )
    marked = await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.project_id == project_id, n.c.kind == "notice", n.c.read_at.is_(None), done)
    )
    return marked.rowcount or 0


__all__ = [
    "CAUSES",
    "failed_run",
    "hand_again",
    "KINDS",
    "ORCHESTRATOR",
    "REDELIVER",
    "Failed",
    "healthy",
    "healthy_worker",
    "mark_read",
    "redeliver",
    "tier0",
]
