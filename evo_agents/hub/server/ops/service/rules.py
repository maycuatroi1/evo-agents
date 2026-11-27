"""Tier 0 of the queue of operations events (plan hub-steward, criterion a6): what the hub does by itself, with no
model, for a project whose default system turns ops on, in the pass of the queue (``ops.service.queue.ops_pass``) after it
took the project's events.

Redelivery. A chat or task run that failed because of its machine, not its work (``CAUSES``: its checkout, its worker
stopped, its lease lost, the network, a 5xx of the hub, its account's login), is handed again once, as a person did
by hand before: a chat run's mention is said again, by the hub, in its thread (``chats.service.runs.hand_mentions``); a task
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

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ops_queue, runs
from evo_agents.hub.server.chats.models.runs import Hand
from evo_agents.hub.server.chats.repository.store import insert_mentions
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.messages import resolve_mentions
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.runs import chat_run, hand_mentions
from evo_agents.hub.server.ops.models.rules import MAX_CHAIN, ORCHESTRATOR, REDELIVER, Failed, healthy
from evo_agents.hub.server.ops.repository import rules as store
from evo_agents.hub.server.orchestrator import origin_of
from evo_agents.hub.server.profile_store import by_id

log = logging.getLogger(__name__)


async def tier0(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """Run the rules of tier 0 for project ``project_id`` in the caller's transaction, nothing while its ops is off:
    how many runs it handed again, items it settled for an Orchestrator, and notices it marked read."""
    if not (await ops_queue.ops_config(conn, project_id)).enabled:
        return {}
    redelivered, taken = await redeliver(conn, project_id)
    return {"redelivered": redelivered, "orchestrated": taken, "read": await store.mark_read(conn, project_id)}


# Redelivery


async def redeliver(conn: AsyncConnection, project_id: int) -> tuple[int, int]:
    """Hand again each run of a pending item of the project's open batches that failed of a cause of ``CAUSES``, as
    the module's docstring says; (how many it handed, how many an Orchestrator took)."""
    handed = taken = 0
    for failed in await store.failed_items(conn, project_id):
        if await handed_before(conn, failed.run_id):
            continue
        found = await orchestrated(conn, failed.run_id)
        if found == "taken":
            taken += await ops_queue.settle(conn, project_id, by=ORCHESTRATOR, run_ids=[failed.run_id])
            continue
        worker_id = None if found == "wait" else await healthy_worker(conn, project_id, failed)
        if worker_id is None:
            continue
        new_id = await hand_again(conn, failed, worker_id)
        if new_id is not None:
            await ops_queue.settle(conn, project_id, by=REDELIVER, run_ids=[failed.run_id])
            log.info("run handed again", extra={"run_id": failed.run_id, "next": new_id, "cause": failed.cause})
            handed += 1
    return handed, taken


async def handed_before(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the hub handed again run ``run_id``, or a run it goes on from (``earlier``, back MAX_CHAIN runs)."""
    seen, todo = set(), {run_id}
    while todo and len(seen) < MAX_CHAIN:
        current = todo.pop()
        seen.add(current)
        todo |= await store.earlier(conn, current) - seen
    return await store.redelivered_any(conn, list(seen))


async def orchestrated(conn: AsyncConnection, run_id: int) -> str | None:
    """``taken`` when an Orchestrator answered the thread of rule failed of run ``run_id`` (an outcome, or its run
    ended done), ``wait`` while its run goes on; None when no thread holds the run, or its run ended otherwise."""
    rows = await store.failed_threads(conn, run_id)
    if any(row.outcome is not None or row.state == "done" for row in rows):
        return "taken"
    if any(row.state is not None and row.state not in runs.TERMINAL_STATES for row in rows):
        return "wait"
    return None


async def healthy_worker(conn: AsyncConnection, project_id: int, failed: Failed) -> int | None:
    """The worker the work of ``failed`` goes to again: its own when a heartbeat after the failure says it is healthy
    (``healthy``), else another online one that is, of the same member and pool, serving the project; None when
    none is."""
    leased = await ops_queue.run_leased(conn, failed.run_id, failed.runtime)
    rows = await store.worker_rows(conn, project_id, failed)
    fit = [row.id for row in rows if healthy(row.health, failed.runtime, leased=leased)]
    if failed.worker_id in fit:
        return failed.worker_id
    return fit[0] if fit else None


async def hand_again(conn: AsyncConnection, failed: Failed, worker_id: int | None) -> int | None:
    """Hand the work of ``failed`` again, to ``worker_id`` (any worker that fits, for None) for a new session, in a
    savepoint undone when it hands none; the run that took it, or None."""
    savepoint = await conn.begin_nested()
    try:
        if failed.kind == "chat":
            new_id = await chat_again(conn, failed, worker_id)
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


async def chat_again(conn: AsyncConnection, failed: Failed, worker_id: int | None) -> int | None:
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
