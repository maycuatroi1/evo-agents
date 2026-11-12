"""The firing of cron jobs (plan hub-cron-jobs, criteria a2 to a6; ``docs/hub.md``, Cron jobs): ``cron_pass``, which
the hub's job worker runs each minute as ``hub.cron_jobs`` (``evo_agents.hub.worker``, handed down by
``HubContext.cron_pass`` since the worker imports no module of ``evo_agents.hub.server``).

A pass takes each job not paused of a project not archived (``cron_store.live_ids``), in a transaction of its own
with the job's row locked (``FOR UPDATE SKIP LOCKED``: a job another worker holds is its), and:

- records the minutes the job was due at since its latest fire of the schedule, its creation or its resume, whichever
  came last, at most a day back (``cron.fires_between``). The latest one, when it is within CATCH_UP of now, fires;
  the others are recorded ``missed``, at most MAX_MISSED of them. Each (job, minute) has one fire
  (``cron_fires_job_id_scheduled_for_key``), so a tick that overlaps another, a second worker or a restart fires a
  minute once: a fire that exists already is left alone;
- handles each fire not handled yet, the "run now" presses included, the oldest first.

A fire first checks the owner: still a writer of the project and on a live seat. When not, the job pauses, the owner
gets a ``cron_paused`` notice, and the fire is ``refused``. Then a run of the job still queued or running, or, for a
job of kind plan, an active run of the plan, makes the fire ``busy``: it queues nothing. Otherwise it starts the work
as the owner, in a savepoint, and a refusal of the hub's checks is the fire's reason, ``refused``:

- ``agent``: a hub message in a new thread of the job's chat (the owner's direct chat with the agent when it names
  none) that names the job and the owner, holds the prompt and mentions the agent, handed on as the owner's own
  mention is (``chat_runs.hand_mentions``: role, mentioned_by, seat, pool, the agent in the chat). A refusal is said in
  the thread too;
- ``plan``: a plan run, with the checks of ``hub run plan`` (``dispatch.queue_plan_run``). A plan with no pending step
  pauses the job;
- ``command``: a command run (``command_dispatch.queue_command_run``), refused when no worker of the owner's serves
  the project.

The runs it queues carry ``cron_job_id`` and are dispatched ``via`` machine, as the owner's tokens dispatch. The
breaker: when the latest BREAKER_FAILURES runs of a job that ended since its creation or resume all failed, of a cause
of the job's (a cause of ``curator.INFRA_CAUSES`` is not counted, as for the night shift), the job pauses and its owner
gets a ``cron_paused`` notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, command_runs, cron, cron_store, curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.agent_tasking import project_role
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply
from evo_agents.hub.server.chat_store import insert_mentions, insert_message
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.command_dispatch import command_workers, queue_command_run
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.runs.models import PlanRunDispatch
from evo_agents.hub.server.runs.service.dispatch import NO_PENDING, queue_plan_run
from evo_agents.hub.server.runs.service.views import plan_activity
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

CATCH_UP = timedelta(minutes=60)  # a minute missed longer ago than this is recorded missed, never fired
LOOK_BACK = timedelta(days=1)  # how far back a pass looks for the minutes a job was due at
MAX_MISSED = 100  # the missed minutes a pass records of a job, the latest ones
BREAKER_FAILURES = 3  # the runs of a job that failed in a row, of its own causes, that pause it
PAUSED_NOTICE = "cron_paused"  # runs.NOTICE_KINDS
VIA = "machine"  # the dispatched_via of the runs a cron job queues
MISSED = "the hub was not firing jobs at this minute, and a later one is due or it is past the catch-up window"


@dataclass
class Outcome:
    """What a fire came to: ``started`` with its run, or ``busy``, ``missed`` or ``refused`` with why; ``pause`` the
    reason the job pauses for, when it does."""

    outcome: str
    reason: str | None = None
    run_id: int | None = None
    pause: str | None = None


def _owner(job: cron_store.Job) -> Principal:
    """The job's owner as the principal its runs are dispatched as: a machine credential of no token."""
    return Principal(job.owner_id, job.owner, False, None, MACHINE, "")  # type: ignore[arg-type]


async def cron_pass(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """Fire the cron jobs due (the module's docstring); the count of each outcome, and of jobs paused."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    async with engine.connect() as conn:
        ids = await cron_store.live_ids(conn)
    counts: dict[str, int] = {}
    for job_id in ids:
        try:
            async with engine.begin() as conn:
                done = await _job_pass(conn, job_id, now)
        except Exception:
            log.exception("cron job could not be fired", extra={"cron_job_id": job_id})
            done = ["error"]
        for outcome in done:
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


async def _job_pass(conn: AsyncConnection, job_id: int, now: datetime) -> list[str]:
    job = await cron_store.held(conn, job_id)
    if job is None or job.paused_at is not None:
        return []
    done: list[str] = ["missed"] * await _due(conn, job, now)
    tripped = await _breaker(conn, job)
    if tripped is not None:
        await _pause(conn, job, tripped)
        done.append("paused")
    pending = await cron_store.pending_fires(conn, job.id)
    paused = tripped
    for fire in pending:
        if paused is not None:
            outcome = Outcome("refused", f"the job is paused: {paused}")
        else:
            outcome = await _fire(conn, job, fire)
            if outcome.pause is not None:
                paused = outcome.pause
                await _pause(conn, job, paused)
                done.append("paused")
        await cron_store.settle_fire(conn, fire["id"], outcome.outcome, outcome.reason, outcome.run_id)
        done.append(outcome.outcome)
        log.info(
            "cron job fired",
            extra={
                "cron_job_id": job.id,
                "outcome": outcome.outcome,
                "run_id": outcome.run_id,
                "manual": fire["manual"],
            },
        )
    return done


async def _due(conn: AsyncConnection, job: cron_store.Job, now: datetime) -> int:
    """Record the minutes ``job`` was due at since its last fire of the schedule: the latest within CATCH_UP to fire,
    the others missed; how many it recorded missed."""
    minute = now.replace(second=0, microsecond=0)
    last = await cron_store.last_scheduled(conn, job.id)
    since = max(at for at in (last, job.created_at, job.resumed_at, minute - LOOK_BACK) if at is not None)
    due = cron.fires_between(job.cron, job.timezone, since, minute)
    latest = due[-1] if due and due[-1] > minute - CATCH_UP else None
    missed = 0
    for at in [at for at in due if at != latest][-MAX_MISSED:]:
        fire = await cron_store.add_fire(conn, job.id, at)
        if fire is not None:
            await cron_store.settle_fire(conn, fire["id"], "missed", MISSED)
            missed += 1
    if latest is not None:
        await cron_store.add_fire(conn, job.id, latest)  # None: a fire of that minute exists, handled or pending
    return missed


async def _owner_refusal(conn: AsyncConnection, job: cron_store.Job) -> str | None:
    """Why the job's owner may no longer have it fire: not a writer of its project, or their seat ended."""
    role = await project_role(conn, job.owner_id, job.project_id)
    if not has_role(role, "writer"):
        return f"its owner {job.owner} is no longer a writer of project {job.project}"
    return await ended_refusal(conn, job.owner_id, job.owner, what=f"cron job {job.name}")


async def _busy(conn: AsyncConnection, job: cron_store.Job) -> str | None:
    r = tables.runs
    query = (
        select(r.c.id, r.c.state)
        .where(r.c.cron_job_id == job.id, r.c.state.in_(runs.ACTIVE_STATES))
        .order_by(r.c.id)
        .limit(1)
    )
    active = (await conn.execute(query)).one_or_none()
    if active is not None:
        return f"run #{active.id} of the job's last fire is {active.state}"
    if job.kind == "plan" and job.plan_id is not None:
        activity = await plan_activity(conn, job.project_id, job.plan_id)
        if activity.plan_run is not None:
            return f"plan {job.plan_id} has plan run #{activity.plan_run.id}, {activity.plan_run.state}"
        if activity.steps:
            step = min(activity.steps.values(), key=lambda run: run.id)
            return f"plan {job.plan_id} has run #{step.id} of a step, {step.state}"
    return None


async def _fire(conn: AsyncConnection, job: cron_store.Job, fire: dict) -> Outcome:
    refusal = await _owner_refusal(conn, job)
    if refusal is not None:
        return Outcome("refused", refusal, pause=refusal)
    busy = await _busy(conn, job)
    if busy is not None:
        return Outcome("busy", busy)
    try:
        async with conn.begin_nested():
            return await START[job.kind](conn, job)
    except HTTPException as exc:
        detail = str(exc.detail)
        pause = detail if job.kind == "plan" and NO_PENDING in detail else None
        return Outcome("refused", detail, pause=pause)
    except ValueError as exc:  # a check of runs: the command line or its time limit
        return Outcome("refused", str(exc))


async def _agent(conn: AsyncConnection, job: cron_store.Job) -> Outcome:
    profile = None if job.profile_id is None else await by_id(conn, job.profile_id)
    if profile is None or profile.deleted_at is not None:
        return Outcome("refused", f"the job's agent @{job.agent or '?'} is gone")
    owner = _owner(job)
    chat_id = job.chat_id if job.chat_id is not None else (await direct_chat(conn, owner, profile.handle))[0]
    chat = await chat_access(conn, owner, chat_id)
    text = runs.clip(f"Cron job {job.name} of {job.owner}:\n\n{job.prompt or ''}", chats.MAX_MESSAGE_BYTES - 200)
    body = f"{text}\n\n@{profile.handle}"
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
    kept, _, _ = await resolve_mentions(conn, chat, owner, [profile.handle])
    hand = Hand(chat, message_id, message_id, profile, owner.user_id, owner.login, VIA, 1, "the hub", body)
    refusals = await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals += [mention["refusal"] for mention in kept if mention["refusal"] and mention["refusal"] not in refusals]
    if refusals:
        await hub_reply(conn, chat.chat_id, message_id, "\n".join(refusals))
    run_id = next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)
    if run_id is None:
        return Outcome("refused", "\n".join(refusals) or f"@{profile.handle} took no work")
    r = tables.runs
    await conn.execute(r.update().where(r.c.id == run_id).values(cron_job_id=job.id))
    return Outcome("started", run_id=run_id)


async def _plan(conn: AsyncConnection, job: cron_store.Job) -> Outcome:
    body = PlanRunDispatch(plan_id=job.plan_id or "")
    run_id = await queue_plan_run(conn, _owner(job), job.project, body, cron_job_id=job.id)
    return Outcome("started", run_id=run_id)


async def _command(conn: AsyncConnection, job: cron_store.Job) -> Outcome:
    if not await command_workers(conn, job.project_id, job.owner_id, VIA):
        return Outcome(
            "refused",
            f"no worker of {job.owner}'s serves project {job.project} and runs command runs: start one with "
            "`evo-agents worker start`, on a release that runs them",
        )
    run_id = await queue_command_run(
        conn,
        project_id=job.project_id,
        owner_id=job.owner_id,
        command=job.command or "",
        repo=job.repo,
        timeout_min=max(command_runs.MIN_TIMEOUT_MINUTES, job.timeout_minutes or 30),
        via=VIA,
        title=command_runs.title_of(job.name, job.command or ""),
        cron_job_id=job.id,
    )
    return Outcome("started", run_id=run_id)


START = {"agent": _agent, "plan": _plan, "command": _command}


async def _breaker(conn: AsyncConnection, job: cron_store.Job) -> str | None:
    """Why ``job`` pauses: its latest BREAKER_FAILURES runs ended since its creation or resume failed, each of a cause
    of the job's; None otherwise."""
    r = tables.runs
    since = job.resumed_at or job.created_at
    query = (
        select(r.c.id, r.c.state, r.c.failure_cause)
        .where(r.c.cron_job_id == job.id, r.c.state.in_(("done", "failed")), r.c.queued_at >= since)
        .order_by(r.c.finished_at.desc().nulls_last(), r.c.id.desc())
        .limit(50)
    )
    ended = [
        row
        for row in await conn.execute(query)
        if not (row.state == "failed" and curator.is_infra(row.failure_cause))  # a dependency's failure, not the job's
    ][:BREAKER_FAILURES]
    if len(ended) < BREAKER_FAILURES or any(row.state != "failed" for row in ended):
        return None
    named = ", ".join(f"#{row.id}" for row in reversed(ended))
    return (
        f"its last {BREAKER_FAILURES} runs failed in a row ({named}); resume it with `evo-agents hub cron resume` once "
        "the cause is fixed"
    )


async def _pause(conn: AsyncConnection, job: cron_store.Job, reason: str) -> None:
    """Pause ``job`` as the hub, for ``reason``, and tell its owner."""
    await cron_store.pause(conn, job.id, None, reason)
    await notify(
        conn,
        user_id=job.owner_id,
        kind="notice",
        notice_kind=PAUSED_NOTICE,
        project_id=job.project_id,
        title=f"Cron job {job.name} of {job.project} paused",
        body=f"The hub paused cron job {job.name}: {reason}.",
        details={"project": job.project, "cron_job": job.name},
    )
