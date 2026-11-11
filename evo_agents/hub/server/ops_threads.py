"""The ops threads of a project's default system (plan hub-steward, criteria a8 to a10): what the hub does, with no
model call, with a batch of the ops queue (``evo_agents.hub.ops_queue``) that closed with items left, in the pass of
the queue (``ops_queue.ops_pass``) after it closed the batches whose time came.

Threads. For each ``ready`` batch the hub opens a thread (``orchestrator.open_ops``, rule ops, key ``ops:batch:<id>``)
in the ops chat ``Ops: <slug>`` of the project's default system, as the person who owns the system (who wrote it),
and mentions the system's Orchestrator (the profile of its orchestrator node) with a summary of the batch: each item,
its cause, a diagnosis, and the command of ``evo-agents hub inbox`` that shows it; no transcript. One thread a
batch, so a new session a batch; the Orchestrator runs by its profile (a worker's runtime or the hub's) under the
autonomy card of the system's owner, the person at the origin of its work. The batch is then ``running``, its run
and thread kept. A batch whose cause key closed ``RECURRING_TIMES`` batches in ``RECURRING_DAYS`` days is
``recurring``: the summary asks the Orchestrator to fix the system (a finding for the Curator, or a task) rather than
run the work again. In the
thread the Orchestrator acts by the ops actions (``ops_actions``) and the frame of ``orchestrator_actions``, with at
most one decision of the owner a batch.

Cap and breaker. The runs of the ops threads of a default system spend at most ``limits.ops_daily_usd`` in 24 hours
(``system_spec.limit_of``): at the cap the hub opens no thread, keeps the batch ``ready``, and sends the project's owner
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
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import ops_queue, runs, system_spec, tables, triage_rules
from evo_agents.hub.server import ops_rules, orchestrator, system_store, system_traces
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.profile_store import by_handle

log = logging.getLogger(__name__)

CAP_NOTICE, PAUSED_NOTICE, DIGEST_NOTICE = "ops_cap", "ops_paused", "ops_digest"  # runs.NOTICE_KINDS
RECURRING_TIMES, RECURRING_DAYS = 3, 7  # a cause key that closed this many batches in these days is recurring
BREAKER_FAILURES = 2  # ops runs in a row failed of their agent that pause a system's ops
CAP_HOURS = 24  # the window of limits.ops_daily_usd, and of the ops_cap notice
ITEMS_SHOWN = 30  # items a summary names
DIAGNOSIS_CHARS = 300
KEY = "ops:batch:{id}"  # orchestrator_threads.key of a batch's thread
ACTIONS_LINE = (
    "Act with `evo-agents worker ops rerun|redeliver|answer-wait|curator-resume|read|finding` (on the hub runtime, "
    "the tool ops_action), give work to an agent with `evo-agents worker triage reassign`, and ask the owner at most "
    "one decision for this batch with `evo-agents worker triage ask-owner`. Never decide for the owner what is "
    "theirs: credentials, scope, spending over the cap, or what cannot be undone without a backup."
)
RECURRING_LINE = (
    "Recurring: this cause key closed {times} batches in {days} days. Fix the system rather than run the work again: "
    "write a finding for the Curator (`evo-agents worker ops finding`) or give the fix to an agent as a task."
)


@dataclass(frozen=True)
class OpsSystem:
    """The default system of a project as the ops threads read it, with its owner: the person who wrote it (the
    project's creator for a system of none), as ``hub_runtime`` tells about its cap."""

    ref: system_traces.SystemRef
    paused: bool
    failures: int
    owner: int
    lead: str | None  # the profile of its orchestrator node


def lead_of(spec: dict | None) -> str | None:
    """The Orchestrator of a system: the profile of its orchestrator node; None for none."""
    for node in (spec or {}).get("nodes") or ():
        if isinstance(node, dict) and node.get("kind") == "orchestrator" and node.get("profile"):
            return str(node["profile"])
    return None


async def default_of(conn: AsyncConnection, project_id: int, *, lock: bool = False) -> OpsSystem | None:
    """The default system of project ``project_id``, locked when ``lock``; None when it has none."""
    s, p = tables.agent_systems, tables.projects
    query = (
        select(
            s.c.id,
            s.c.slug,
            s.c.revision,
            s.c.spec,
            s.c.ops_paused_at,
            s.c.ops_failures,
            func.coalesce(s.c.created_by, p.c.created_by).label("owner"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .where(s.c.project_id == project_id, s.c.is_default)
    )
    if lock:
        query = query.with_for_update(of=s)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    ref = system_traces.SystemRef(row.id, row.slug, project_id, row.revision, row.spec)
    return OpsSystem(ref, row.ops_paused_at is not None, row.ops_failures, row.owner, lead_of(row.spec))


def _thread_runs(system_id: int):
    """The runs of the ops threads of system ``system_id``: id, state, failure cause, the thread's id."""
    r, m, t = tables.runs, tables.chat_messages, tables.orchestrator_threads
    return (
        select(r.c.id, r.c.state, r.c.failure_cause, t.c.id.label("thread_id"))
        .select_from(
            r.join(m, m.c.id == r.c.chat_message_id).join(
                t,
                and_(t.c.chat_id == r.c.chat_id, t.c.root_message_id == func.coalesce(m.c.thread_root_id, m.c.id)),
            )
        )
        .where(t.c.system_id == system_id, t.c.rule == triage_rules.OPS)
    )


async def spent(conn: AsyncConnection, system_id: int) -> float:
    """What the runs of the ops threads of system ``system_id`` queued in the last CAP_HOURS spent (run_spend)."""
    rs, r = tables.run_spend, tables.runs
    inside = _thread_runs(system_id).where(r.c.queued_at >= func.now() - timedelta(hours=CAP_HOURS)).subquery()
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    query = select(cost).where(rs.c.run_id.in_(select(inside.c.id)))
    return float((await conn.execute(query)).scalar_one() or 0)


async def _told(conn: AsyncConnection, owner: int, kind: str, project_id: int, since) -> bool:
    n = tables.notifications
    query = select(n.c.id).where(
        n.c.user_id == owner, n.c.notice_kind == kind, n.c.project_id == project_id, n.c.created_at >= since
    )
    return (await conn.execute(query.limit(1))).first() is not None


async def _tell_cap(conn: AsyncConnection, system: OpsSystem, used: float, cap: float) -> None:
    project_id = system.ref.project_id
    if await _told(conn, system.owner, CAP_NOTICE, project_id, func.now() - timedelta(hours=CAP_HOURS)):
        return
    await notify(
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


def _items(batch_id: int):
    i, r, ap, d, w = tables.ops_items, tables.runs, tables.agent_profiles, tables.decisions, tables.workers
    return (
        select(
            i.c.id,
            i.c.kind,
            i.c.cause,
            i.c.run_id,
            i.c.decision_id,
            i.c.worker_id,
            r.c.kind.label("run_kind"),
            r.c.state.label("run_state"),
            r.c.error,
            ap.c.handle,
            d.c.question,
            d.c.category,
            w.c.name.label("worker"),
        )
        .select_from(
            i.outerjoin(r, r.c.id == i.c.run_id)
            .outerjoin(ap, ap.c.id == r.c.profile_id)
            .outerjoin(d, d.c.id == i.c.decision_id)
            .outerjoin(w, w.c.id == i.c.worker_id)
        )
        .where(i.c.batch_id == batch_id, i.c.state == "pending")
        .order_by(i.c.id)
    )


def _flat(text: str | None, fallback: str) -> str:
    return runs.clip(" ".join(str(text or fallback).split()), DIAGNOSIS_CHARS)


def item_line(row) -> str:
    """One item of a batch's summary: what it is, its cause, a diagnosis, and the inbox command that shows it."""
    if row.decision_id is not None:
        what = f"decision #{row.decision_id} ({row.category or 'decision'}) waits: {_flat(row.question, 'no question')}"
        shown = f"evo-agents hub inbox --show d{row.decision_id}"
    elif row.run_id is not None:
        agent = f", @{row.handle}" if row.handle else ""
        state = row.run_state or "gone"
        error = _flat(row.error, "its worker gave no reason")
        what = f"run #{row.run_id} ({row.run_kind or 'run'}{agent}) {state}: {error}"
        shown = f"evo-agents hub inbox --show r{row.run_id}"
    elif row.worker_id is not None:
        what = f"worker #{row.worker_id} ({row.worker or 'gone'}) reported its health: {row.cause}"
        shown = "evo-agents hub inbox"
    else:
        what = f"{row.kind}: {row.cause}"
        shown = "evo-agents hub inbox"
    return f"- {row.kind}, cause {row.cause}: {what}. Next: `{shown}`"


async def recurring_times(conn: AsyncConnection, project_id: int, key: str) -> int:
    """How many batches of cause key ``key`` of the project closed in the last RECURRING_DAYS days with items left."""
    b = tables.ops_batches
    query = select(func.count()).where(
        b.c.project_id == project_id,
        b.c.key == key,
        b.c.state.in_(("ready", "running", "done")),
        b.c.closed_at >= func.now() - timedelta(days=RECURRING_DAYS),
    )
    return int((await conn.execute(query)).scalar_one())


async def summary(conn: AsyncConnection, batch, times: int) -> str:
    """The first message of the thread of ``batch``: the rule, the batch, the recurring line, its items, the actions."""
    rows = (await conn.execute(_items(batch.id))).all()
    lines = [
        f"Rule: {triage_rules.OPS}: {triage_rules.RULE_TEXT[triage_rules.OPS]}.",
        f"Batch #{batch.id}: {len(rows)} items of {batch.kind}, cause {batch.cause}, {batch.subject} "
        f"(key {batch.key}).",
    ]
    if times >= RECURRING_TIMES:
        lines.append(RECURRING_LINE.format(times=times, days=RECURRING_DAYS))
    lines += [item_line(row) for row in rows[:ITEMS_SHOWN]]
    if len(rows) > ITEMS_SHOWN:
        lines.append(f"... and {len(rows) - ITEMS_SHOWN} more: `evo-agents hub system ops batches`.")
    return "\n".join([*lines, ACTIONS_LINE])


# Opening and settling


def _ready(project_id: int):
    b = tables.ops_batches
    query = select(b).where(b.c.project_id == project_id, b.c.state == "ready").order_by(b.c.id)
    return query.with_for_update(skip_locked=True)


async def open_batch(conn: AsyncConnection, system: OpsSystem, batch) -> str:
    """Open the thread of ready ``batch`` of ``system``'s project, as the module's docstring says: ``opened``,
    ``capped``, or why it stays ready (``paused``, ``no_lead``, ``refused``)."""
    if system.paused:
        return "paused"
    lead = None if system.lead is None else await by_handle(conn, system.lead)
    if lead is None or lead.deleted_at is not None:
        return "no_lead"
    cap = float(system_spec.limit_of(system.ref.spec, "ops_daily_usd"))
    used = await spent(conn, system.ref.id)
    if used >= cap:
        await _tell_cap(conn, system, used, cap)
        return "capped"
    times = await recurring_times(conn, system.ref.project_id, batch.key)
    text = await summary(conn, batch, times)
    person = await orchestrator.machine_person(conn, system.owner)
    async with conn.begin_nested():
        opened = await orchestrator.open_ops(conn, system.ref, lead, person, KEY.format(id=batch.id), text)
    if opened is None:
        return "refused"
    b = tables.ops_batches
    values = {"state": "running", "run_id": opened.run_id, "thread_id": opened.thread_id}
    await conn.execute(update(b).values(**values, recurring=times >= RECURRING_TIMES).where(b.c.id == batch.id))
    log.info("ops batch opened", extra={"batch_id": batch.id, "thread_id": opened.thread_id, "run_id": opened.run_id})
    return "opened"


def agent_failed(state: str | None, cause: str | None) -> bool:
    """Whether a run that ended ``state`` with ``cause`` failed of its agent, not of its machine."""
    return state == "failed" and cause not in ops_queue.WORKER_CAUSES


async def _ended(conn: AsyncConnection, system_id: int, thread_id: int | None):
    """(the last run of thread ``thread_id``, ended), or None while one of its runs goes on; (None, None) for a
    thread with no run."""
    if thread_id is None:
        return None, None
    inside = _thread_runs(system_id).where(tables.orchestrator_threads.c.id == thread_id).subquery()
    rows = (await conn.execute(select(inside).order_by(inside.c.id))).all()
    if any(row.state not in runs.TERMINAL_STATES for row in rows):
        return None
    return (rows[-1], True) if rows else (None, None)


async def settle_batch(conn: AsyncConnection, system: OpsSystem, batch) -> str | None:
    """Mark running ``batch`` done once the runs of its thread ended, counting a failure of the agent toward the
    breaker; the state of the system's ops it left (``paused``, ``counted``, ``reset``, ``done``), or None while a run
    goes on."""
    ended = await _ended(conn, system.ref.id, batch.thread_id)
    if ended is None:
        return None
    last, _ = ended
    b, s = tables.ops_batches, tables.agent_systems
    await conn.execute(update(b).values(state="done").where(b.c.id == batch.id))
    if last is None:
        return "done"
    if not agent_failed(last.state, last.failure_cause):
        if last.state == "done":
            await conn.execute(update(s).values(ops_failures=0).where(s.c.id == system.ref.id))
            return "reset"
        return "done"
    failures = system.failures + 1
    if failures < BREAKER_FAILURES:
        await conn.execute(update(s).values(ops_failures=failures).where(s.c.id == system.ref.id))
        return "counted"
    await trip(conn, system, last.id)
    return "paused"


async def trip(conn: AsyncConnection, system: OpsSystem, run_id: int) -> None:
    """Pause the ops of ``system``, whose ops runs failed of their agent BREAKER_FAILURES times in a row, and tell
    the project's owner."""
    reason = f"{BREAKER_FAILURES} ops runs in a row failed of their agent, the last run #{run_id}"
    await system_store.set_paused(conn, system.ref.id, reason)
    s = tables.agent_systems
    await conn.execute(update(s).values(ops_failures=0).where(s.c.id == system.ref.id))
    await notify(
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
    system = await default_of(conn, project_id, lock=True)
    if system is None or not ops_queue.read_ops(system.ref.spec).enabled:
        return did
    b = tables.ops_batches
    running = select(b).where(b.c.project_id == project_id, b.c.state == "running").order_by(b.c.id)
    for batch in (await conn.execute(running.with_for_update(skip_locked=True))).all():
        settled = await settle_batch(conn, system, batch)
        if settled is not None:
            did[f"settled_{settled}"] += 1
            system = await default_of(conn, project_id) or system
    for batch in (await conn.execute(_ready(project_id))).all():
        state = await open_batch(conn, system, batch)
        did[state] += 1
        if state in ("paused", "capped", "no_lead"):
            break
    if await digest(conn, system):
        did["digest"] += 1
    return did


async def threads_pass(engine: AsyncEngine) -> dict[str, int]:
    """``project_pass`` for each project whose default system turns ops on, a transaction each; what it did."""
    s = tables.agent_systems
    on = select(s.c.project_id).where(s.c.is_default, s.c.spec["ops"]["enabled"].as_boolean().is_(True))
    async with engine.connect() as conn:
        projects = sorted((await conn.execute(on)).scalars().all())
    did = Counter[str]()
    for project_id in projects:
        try:
            async with engine.begin() as conn:
                did.update(await project_pass(conn, project_id))
        except Exception:  # one project's failure leaves the others their pass
            log.exception("ops threads pass failed", extra={"project_id": project_id})
    return dict(did)


# The digest


@dataclass(frozen=True)
class Digest:
    """What the ops of a project did since ``since`` and what waits on a person."""

    tier0: int
    orchestrator: int
    threads: int
    waiting: list[str]

    def empty(self) -> bool:
        return not (self.tier0 or self.orchestrator or self.threads or self.waiting)


async def _since(conn: AsyncConnection, owner: int, project_id: int):
    n = tables.notifications
    last = select(func.max(n.c.created_at)).where(
        n.c.user_id == owner, n.c.notice_kind == DIGEST_NOTICE, n.c.project_id == project_id
    )
    found = (await conn.execute(last)).scalar_one_or_none()
    floor = (await conn.execute(select(func.now() - timedelta(hours=24)))).scalar_one()
    return floor if found is None or found < floor else found


async def _waiting(conn: AsyncConnection, system: OpsSystem) -> list[str]:
    """What of the project's ops waits on a person, and why."""
    b, t, d = tables.ops_batches, tables.orchestrator_threads, tables.decisions
    project_id, lines = system.ref.project_id, []
    held = select(func.count()).where(b.c.project_id == project_id, b.c.state == "ready")
    count = int((await conn.execute(held)).scalar_one())
    if count:
        why = "its ops are paused" if system.paused else "the cap or no Orchestrator holds them"
        lines.append(f"{count} batches wait in the queue: {why} (`evo-agents hub system ops show {system.ref.slug}`)")
    asked = (
        select(d.c.id, d.c.question)
        .join_from(d, t, t.c.orchestrator_run_id == d.c.run_id)
        .where(t.c.project_id == project_id, t.c.rule == triage_rules.OPS, d.c.state == "open")
        .order_by(d.c.id)
    )
    for row in (await conn.execute(asked)).all():
        lines.append(
            f"decision #{row.id} of the Orchestrator waits on you: {_flat(row.question, '')} "
            f"(`evo-agents hub inbox --show d{row.id}`)"
        )
    i = tables.ops_items
    left = (
        select(func.count())
        .select_from(i.join(b, b.c.id == i.c.batch_id))
        .where(i.c.project_id == project_id, i.c.state == "pending", b.c.state == "done")
    )
    count = int((await conn.execute(left)).scalar_one())
    if count:
        lines.append(f"{count} items the Orchestrator's runs left as they were: `evo-agents hub inbox`")
    return lines


async def digest_of(conn: AsyncConnection, system: OpsSystem, since) -> Digest:
    """What the ops of ``system``'s project did since ``since`` and what waits on a person."""
    i, t = tables.ops_items, tables.orchestrator_threads
    project_id = system.ref.project_id
    handled = (
        select(i.c.handled_by, func.count())
        .where(i.c.project_id == project_id, i.c.state == "handled", i.c.handled_at >= since)
        .group_by(i.c.handled_by)
    )
    by = {row[0]: row[1] for row in await conn.execute(handled)}
    threads = select(func.count()).where(
        t.c.project_id == project_id, t.c.rule == triage_rules.OPS, t.c.created_at >= since
    )
    return Digest(
        tier0=int(by.get(ops_rules.REDELIVER, 0)),
        orchestrator=int(by.get(ops_rules.ORCHESTRATOR, 0)),
        threads=int((await conn.execute(threads)).scalar_one()),
        waiting=await _waiting(conn, system),
    )


async def digest(conn: AsyncConnection, system: OpsSystem) -> bool:
    """Send the project's owner the ``ops_digest`` of the day, once a day (UTC) and only when there is something to
    tell; whether it sent one."""
    project_id = system.ref.project_id
    if await _told(conn, system.owner, DIGEST_NOTICE, project_id, func.date_trunc("day", func.now())):
        return False
    found = await digest_of(conn, system, await _since(conn, system.owner, project_id))
    if found.empty():
        return False
    body = [
        f"Tier 0 handed {found.tier0} runs again that failed of their machine.",
        f"The Orchestrator read {found.threads} batches in ops threads and handled {found.orchestrator} items.",
    ]
    body += ["Waits on you:", *(f"- {line}" for line in found.waiting)] if found.waiting else ["Nothing waits on you."]
    await notify(
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


__all__ = [
    "BREAKER_FAILURES",
    "CAP_NOTICE",
    "DIGEST_NOTICE",
    "KEY",
    "PAUSED_NOTICE",
    "RECURRING_DAYS",
    "RECURRING_TIMES",
    "Digest",
    "OpsSystem",
    "agent_failed",
    "default_of",
    "digest",
    "digest_of",
    "item_line",
    "lead_of",
    "open_batch",
    "project_pass",
    "recurring_times",
    "settle_batch",
    "spent",
    "summary",
    "threads_pass",
    "trip",
]
