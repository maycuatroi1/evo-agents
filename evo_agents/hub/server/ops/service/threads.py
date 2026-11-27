"""The ops threads of a project's default system (plan hub-steward, criteria a8 to a10): what the hub does, with no
model call, with a batch of the ops queue (``evo_agents.hub.ops_queue``) that closed with items left, in the pass of
the queue (``ops.service.queue.ops_pass``) after it closed the batches whose time came.

Threads. For each ``ready`` batch the hub opens a thread (``orchestrator.open_ops``, rule ops, key ``ops:batch:<id>``)
in the ops chat ``Ops: <slug>`` of the project's default system, as the person who owns the system (who wrote it),
and mentions the system's Orchestrator (the profile of its orchestrator node) with a summary of the batch: each item,
its cause, a diagnosis, and the command of ``evo-agents hub inbox`` that shows it; no transcript. One thread a
batch, so a new session a batch; the Orchestrator runs by its profile (a worker's runtime or the hub's) under the
autonomy card of the system's owner, the person at the origin of its work. The batch is then ``running``, its run
and thread kept. A batch whose cause key closed ``RECURRING_TIMES`` batches in ``RECURRING_DAYS`` days is
``recurring``: the summary asks the Orchestrator to fix the system (a finding for the Curator, or a task) rather than
run the work again. In the
thread the Orchestrator acts by the ops actions (``ops.service.actions``) and the frame of ``orchestrator_actions``, with at
most one decision of the owner a batch.

Cap and breaker. The runs of the ops threads of a default system spend at most ``limits.ops_daily_usd`` in 24 hours
(``system_spec.limit_of``; 0, the default, sets no cap): at the cap the hub opens no thread, keeps the batch ``ready``, and sends the project's owner
one ``ops_cap`` notice a day. A system whose ops are paused (``agent_systems.ops_paused_at``) keeps its batches
``ready`` too. A running batch is ``done`` once the runs of its thread ended; when the last of them failed of its
agent (a cause other than the machine's, ``ops_queue.WORKER_CAUSES``), the system counts it (``ops_failures``), and
at ``BREAKER_FAILURES`` in a row its ops pause (``system_store.set_paused``) and the owner gets an ``ops_paused``
notice; ``evo-agents hub system ops resume`` resumes them. A run done sets the count back to 0.

Digest. Once a day, the project's owner gets an ``ops_digest`` notice of what tier 0 and the Orchestrator handled
since the last one (24 hours at most), and what waits on a person and why; none on a day with nothing.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, ops_queue, runs, system_spec, triage_rules
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.ops.models.rules import ORCHESTRATOR, REDELIVER
from evo_agents.hub.server.ops.models.threads import (
    ACTIONS_LINE,
    BREAKER_FAILURES,
    CAP_HOURS,
    CAP_NOTICE,
    DIGEST_NOTICE,
    ITEMS_SHOWN,
    KEY,
    PAUSED_NOTICE,
    RECURRING_DAYS,
    RECURRING_LINE,
    RECURRING_TIMES,
    Digest,
    OpsSystem,
    flat,
    item_line,
    queued_line,
    stalled_line,
)
from evo_agents.hub.server.ops.repository import threads as store
from evo_agents.hub.server.orchestrator.service import threads as orchestrator
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.runs.service import priority
from evo_agents.hub.server.systems.repository import store as system_store

log = logging.getLogger(__name__)


async def tell_cap(conn: AsyncConnection, system: OpsSystem, used: float, cap: float) -> None:
    """Tell the project's owner, once in CAP_HOURS, that the ops of ``system`` reached their cap."""
    project_id = system.ref.project_id
    if await store.told(conn, system.owner, CAP_NOTICE, project_id, hours=CAP_HOURS):
        return
    await NOTIFIER.notify(
        conn,
        user_id=system.owner,
        kind="notice",
        notice_kind=CAP_NOTICE,
        project_id=project_id,
        title=f"The ops of system {system.ref.slug} reached their cap of ${cap:g} in 24 hours",
        body=(
            f"The runs of its ops threads spent ${used:.2f} in 24 hours, limits.ops_daily_usd is ${cap:g}: its batches "
            "wait in the queue until the spend falls under it. Raise the limit in the system's spec, or read the "
            "batches with `evo-agents hub system ops batches`."
        ),
        details={"system_id": system.ref.id, "slug": system.ref.slug, "spent_usd": round(used, 4), "cap_usd": cap},
    )


# The summary of a batch


async def item_text(conn: AsyncConnection, project_id: int, row) -> str:
    """``item_line`` of ``row``, with what the hub reads of a stalled plan or a run queued too long."""
    if row.kind == hub_events.PLAN_STALLED:
        payload = row.payload or {}
        plan_id = payload.get("plan_id") or row.subject.removeprefix("plan:")
        return stalled_line(row, await store.last_trace(conn, project_id, plan_id))
    if row.kind == hub_events.RUN_QUEUED_LONG and row.run_id is not None:
        if row.worker_id is None:
            ahead = "?"
        else:
            ahead = str(await priority.ahead(conn, row.run_id, row.worker_id)) if row.run_state == "queued" else "0"
        return queued_line(row, ahead)
    return item_line(row)


async def summary(conn: AsyncConnection, batch, times: int) -> str:
    """The first message of the thread of ``batch``: the rule, the batch, the recurring line, its items, the actions."""
    rows = await store.pending_items(conn, batch.id)
    lines = [
        f"Rule: {triage_rules.OPS}: {triage_rules.RULE_TEXT[triage_rules.OPS]}.",
        f"Batch #{batch.id}: {len(rows)} items of {batch.kind}, cause {batch.cause}, {batch.subject} "
        f"(key {batch.key}).",
    ]
    if times >= RECURRING_TIMES:
        lines.append(RECURRING_LINE.format(times=times, days=RECURRING_DAYS))
    lines += [await item_text(conn, batch.project_id, row) for row in rows[:ITEMS_SHOWN]]
    if len(rows) > ITEMS_SHOWN:
        lines.append(f"... and {len(rows) - ITEMS_SHOWN} more: `evo-agents hub system ops batches`.")
    return "\n".join([*lines, ACTIONS_LINE])


# Opening and settling


async def open_batch(conn: AsyncConnection, system: OpsSystem, batch) -> str:
    """Open the thread of ready ``batch`` of ``system``'s project, as the module's docstring says: ``opened``,
    ``capped``, or why it stays ready (``paused``, ``no_lead``, ``refused``)."""
    if system.paused:
        return "paused"
    lead = None if system.lead is None else await by_handle(conn, system.lead)
    if lead is None or lead.deleted_at is not None:
        return "no_lead"
    cap = float(system_spec.limit_of(system.ref.spec, "ops_daily_usd"))
    used = await store.spent(conn, system.ref.id)  # pyright: ignore[reportArgumentType]
    if system_spec.over_usd(used, cap):
        await tell_cap(conn, system, used, cap)
        return "capped"
    times = await store.recurring_times(conn, system.ref.project_id, batch.key)
    text = await summary(conn, batch, times)
    person = await orchestrator.machine_person(conn, system.owner)
    savepoint = await conn.begin_nested()
    try:
        opened = await orchestrator.open_ops(conn, system.ref, lead, person, KEY.format(id=batch.id), text)
    except BaseException:
        await savepoint.rollback()
        raise
    if opened is None or opened.run_id is None:
        # No run took the thread (the lead's pool has no free slot, say): the thread and its refusal go, and the
        # batch stays ready for the next pass instead of settling done with its items unread.
        await savepoint.rollback()
        return "refused"
    await savepoint.commit()
    await store.set_running(conn, batch.id, opened.run_id, opened.thread_id, times >= RECURRING_TIMES)
    log.info("ops batch opened", extra={"batch_id": batch.id, "thread_id": opened.thread_id, "run_id": opened.run_id})
    return "opened"


def agent_failed(state: str | None, cause: str | None) -> bool:
    """Whether a run that ended ``state`` with ``cause`` failed of its agent, not of its machine."""
    return state == "failed" and cause not in ops_queue.WORKER_CAUSES


async def ended(conn: AsyncConnection, system_id: int, thread_id: int | None):
    """(the last run of thread ``thread_id``, ended), or None while one of its runs goes on; (None, None) for a
    thread with no run."""
    if thread_id is None:
        return None, None
    rows = await store.thread_rows(conn, system_id, thread_id)
    if any(row.state not in runs.TERMINAL_STATES for row in rows):
        return None
    return (rows[-1], True) if rows else (None, None)


async def settle_batch(conn: AsyncConnection, system: OpsSystem, batch) -> str | None:
    """Mark running ``batch`` done once the runs of its thread ended, counting a failure of the agent toward the
    breaker; the state of the system's ops it left (``paused``, ``counted``, ``reset``, ``done``), or None while a run
    goes on."""
    found = await ended(conn, system.ref.id, batch.thread_id)  # pyright: ignore[reportArgumentType]
    if found is None:
        return None
    last, _ = found
    await store.set_done(conn, batch.id)
    if last is None:
        return "done"
    if not agent_failed(last.state, last.failure_cause):
        if last.state == "done":
            await store.set_failures(conn, system.ref.id, 0)  # pyright: ignore[reportArgumentType]
            return "reset"
        return "done"
    failures = system.failures + 1
    if failures < BREAKER_FAILURES:
        await store.set_failures(conn, system.ref.id, failures)  # pyright: ignore[reportArgumentType]
        return "counted"
    await trip(conn, system, last.id)
    return "paused"


async def trip(conn: AsyncConnection, system: OpsSystem, run_id: int) -> None:
    """Pause the ops of ``system``, whose ops runs failed of their agent BREAKER_FAILURES times in a row, and tell
    the project's owner."""
    reason = f"{BREAKER_FAILURES} ops runs in a row failed of their agent, the last run #{run_id}"
    await system_store.set_paused(conn, system.ref.id, reason)  # pyright: ignore[reportArgumentType]
    await store.set_failures(conn, system.ref.id, 0)  # pyright: ignore[reportArgumentType]
    await NOTIFIER.notify(
        conn,
        user_id=system.owner,
        kind="notice",
        notice_kind=PAUSED_NOTICE,
        project_id=system.ref.project_id,
        run_id=run_id,
        title=f"The ops of system {system.ref.slug} paused: {BREAKER_FAILURES} runs failed in a row",
        body=(
            f"{reason}. Its batches wait in the queue. Read the runs, fix the Orchestrator or its profile, then "
            f"resume with `evo-agents hub system ops resume {system.ref.slug}`."
        ),
        details={"system_id": system.ref.id, "slug": system.ref.slug, "run_id": run_id},
    )
    log.warning("ops paused by the breaker", extra={"system_id": system.ref.id, "run_id": run_id})


async def project_pass(conn: AsyncConnection, project_id: int) -> Counter[str]:
    """Settle the running batches of project ``project_id``, open the threads of its ready ones, and send its digest;
    what it did, by name."""
    did = Counter[str]()
    system = await store.default_of(conn, project_id, lock=True)
    if system is None or not ops_queue.read_ops(system.ref.spec).enabled:
        return did
    for batch in await store.batches(conn, project_id, "running"):
        settled = await settle_batch(conn, system, batch)
        if settled is not None:
            did[f"settled_{settled}"] += 1
            system = await store.default_of(conn, project_id) or system
    for batch in await store.batches(conn, project_id, "ready"):
        state = await open_batch(conn, system, batch)
        did[state] += 1
        if state in ("paused", "capped", "no_lead"):
            break
    if await digest(conn, system):
        did["digest"] += 1
    return did


async def threads_pass(engine: AsyncEngine) -> dict[str, int]:
    """``project_pass`` for each project whose default system turns ops on, a transaction each; what it did."""
    async with engine.connect() as conn:
        projects = await store.ops_projects(conn)
    did = Counter[str]()
    for project_id in projects:
        try:
            async with engine.begin() as conn:
                did.update(await project_pass(conn, project_id))
        except Exception:  # one project's failure leaves the others their pass
            log.exception("ops threads pass failed", extra={"project_id": project_id})
    return dict(did)


# The digest


async def waiting(conn: AsyncConnection, system: OpsSystem) -> list[str]:
    """What of the project's ops waits on a person, and why."""
    project_id, lines = system.ref.project_id, []
    count = await store.ready_count(conn, project_id)
    if count:
        why = "its ops are paused" if system.paused else "the cap or no Orchestrator holds them"
        lines.append(f"{count} batches wait in the queue: {why} (`evo-agents hub system ops show {system.ref.slug}`)")
    for row in await store.open_decisions(conn, project_id):
        lines.append(
            f"decision #{row.id} of the Orchestrator waits on you: {flat(row.question, '')} "
            f"(`evo-agents hub inbox --show d{row.id}`)"
        )
    count = await store.left_count(conn, project_id)
    if count:
        lines.append(f"{count} items the Orchestrator's runs left as they were: `evo-agents hub inbox`")
    return lines


async def digest_of(conn: AsyncConnection, system: OpsSystem, since) -> Digest:
    """What the ops of ``system``'s project did since ``since`` and what waits on a person."""
    project_id = system.ref.project_id
    by = await store.handled_since(conn, project_id, since)
    return Digest(
        tier0=int(by.get(REDELIVER, 0)),
        orchestrator=int(by.get(ORCHESTRATOR, 0)),
        threads=await store.threads_since(conn, project_id, since),
        waiting=await waiting(conn, system),
    )


async def digest(conn: AsyncConnection, system: OpsSystem) -> bool:
    """Send the project's owner the ``ops_digest`` of the day, once a day (UTC) and only when there is something to
    tell; whether it sent one."""
    project_id = system.ref.project_id
    if await store.told(conn, system.owner, DIGEST_NOTICE, project_id, hours=None):
        return False
    found = await digest_of(conn, system, await store.digest_since(conn, system.owner, project_id))
    if found.empty():
        return False
    body = [
        f"Tier 0 handed {found.tier0} runs again that failed of their machine.",
        f"The Orchestrator read {found.threads} batches in ops threads and handled {found.orchestrator} items.",
    ]
    body += ["Waits on you:", *(f"- {line}" for line in found.waiting)] if found.waiting else ["Nothing waits on you."]
    await NOTIFIER.notify(
        conn,
        user_id=system.owner,
        kind="notice",
        notice_kind=DIGEST_NOTICE,
        project_id=project_id,
        title=f"Ops of system {system.ref.slug}: {len(found.waiting)} things wait on you",
        body="\n".join(body),
        details={
            "system_id": system.ref.id,
            "tier0": found.tier0,
            "orchestrator": found.orchestrator,
            "threads": found.threads,
            "waiting": len(found.waiting),
        },
    )
    return True
