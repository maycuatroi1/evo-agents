"""The waits of plan runs' agents (``evo_agents.hub.server.run_waits`` has the protocol): asking one, and the hub's
answer when its time or its event comes, each transaction opened with UnitOfWork."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, runs
from evo_agents.hub.server import workflow_waits
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.decisions.models import Decision, OptionIn
from evo_agents.hub.server.decisions.service.asks import store_decision
from evo_agents.hub.server.decisions.service.resume import lock_run_of, resume_parked
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models.run_waits import MAX_WAIT, OPTIONS, PAYLOAD_IN_MESSAGE, WaitAsk, WaitIn
from evo_agents.hub.server.runs.repository import run_waits as repository
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.views import lock_plan
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.run_waits")


def wake_time(body: WaitIn, now: datetime) -> datetime:
    """When a wait asked at ``now`` wakes at the latest: ``until``, or MAX_WAIT from now; 422 for a time past or past
    MAX_WAIT."""
    if body.until is None:
        return now + MAX_WAIT
    until = body.until.astimezone(UTC)
    if until <= now:
        raise HTTPException(422, f"until {until:%Y-%m-%dT%H:%M:%SZ} is past: a wait wakes at a time to come")
    if until > now + MAX_WAIT:
        latest = now + MAX_WAIT
        raise HTTPException(
            422,
            f"until is at most {runs.MAX_WAIT_DAYS} days ahead ({latest:%Y-%m-%dT%H:%MZ}): wait that long, and wait "
            "again when it wakes",
        )
    return until


def _context(project: str, wake_at: datetime, body: WaitIn) -> str:
    """The context of a wait: what the run waits for, and what an answer of the person asked does."""
    lines = ["## What the run waits for", ""]
    if body.on is not None:
        contains = f" whose payload contains `{json.dumps(body.contains, ensure_ascii=False)}`" if body.contains else ""
        lines.append(f"- An event `{body.on}` of project {project}{contains}.")
        lines.append(f"- At the latest, its time: {wake_at:%Y-%m-%d %H:%M} UTC.")
    else:
        lines.append(f"- Its time: {wake_at:%Y-%m-%d %H:%M} UTC.")
    lines += [
        "",
        "The hub answers this wait when the first of them comes, and the agent goes on in its session. Nobody needs to "
        "answer it; Go on now wakes the run sooner, and Stop waiting has it go on without what it waits for.",
    ]
    return "\n".join(lines)


def _ask(project: str, wake_at: datetime, body: WaitIn) -> WaitAsk:
    return WaitAsk(
        category=runs.WAIT_DECISION_CATEGORY,
        question=body.reason.strip(),
        context=_context(project, wake_at, body),
        options=[OptionIn(key=key, label=label, description=text) for key, label, text in OPTIONS],
        step_key=body.step_key,
    )


async def wait(engine: AsyncEngine, github_app, user: Principal, run_id: int, body: WaitIn) -> Decision:
    """Wait, in a plan run the worker of ``user`` holds, for a time or an event of the hub: a decision of category
    wait the hub answers when either comes. ``github_app`` reads a workflow run a wait names."""
    wake_at = wake_time(body, datetime.now(UTC))
    if body.on == hub_events.WORKFLOW_RUN:  # a run the hub can read, on a GitHub repo of the project
        async with UnitOfWork(engine) as conn:
            _, row = await held_plan_run(conn, user, run_id)
            repos = await workflow_waits.github_repos(conn, row.project_id)
        body.contains = await workflow_waits.checked(github_app, repos, body.contains)
    async with UnitOfWork(engine) as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
        wake: dict = {"wake_at": wake_at, "wake_on": None}
        if body.on is not None:
            after = await repository.last_event(conn, row.project_id)
            wake["wake_on"] = {"kind": body.on, "contains": body.contains, "after": after}
        view = await store_decision(conn, run_id, row, _ask(row.name, wake_at, body), wake=wake)
    log.info("wait asked", extra={"run_id": run_id, "decision_id": view.id, "on": body.on})
    return view


# Waking


def woke_message(decision_id: int, reason: str, *, time: datetime | None = None, event=None) -> str:
    """The inbox message that wakes the agent of a wait, which is the wait's answer_text too: the time or the event
    that came, with the event's payload, and what it waited for, within MAX_ANSWER_BYTES."""
    if event is not None:
        payload = runs.clip(json.dumps(event.payload, ensure_ascii=False, sort_keys=True), PAYLOAD_IN_MESSAGE)
        came = (
            f"Decision #{decision_id} (wait) woke: event {event.kind} #{event.id} of the project came at "
            f"{event.created_at:%Y-%m-%dT%H:%M:%SZ}, with the payload {payload}."
        )
    else:
        came = f"Decision #{decision_id} (wait) woke: its time came ({time:%Y-%m-%dT%H:%M:%SZ})."
    lines = [
        came,
        f"It waited for: {NOTIFIER.one_line(reason, 500)}",
        *([workflow_waits.WOKE_HINT] if event is not None and event.kind == hub_events.WORKFLOW_RUN else []),
        "Check on the hub that what you waited for is there before you rely on it, and wait again with `evo-agents "
        "worker wait` when it is not.",
    ]
    return runs.clip("\n".join(lines), runs.MAX_ANSWER_BYTES)


async def wake(conn: AsyncConnection, decision_id: int, project_id: int, plan_id: str | None, text: str) -> int | None:
    """Answer wait ``decision_id`` as the hub with ``text``, in the caller's transaction, under the locks an answer
    takes: the message goes to its run's inbox, or to the inbox of the run that resumes it when it is parked; that
    run's id, or None when the wait is no longer open or its run takes no answer. The inbox takes ``text`` whole, up to
    MAX_INBOX_BYTES (an asked agent's answer); the decision keeps it within MAX_ANSWER_BYTES."""
    run_id, run_state, _ = await lock_run_of(conn, project_id, plan_id, decision_id)
    state = await repository.locked_state(conn, decision_id)
    if state != "open" or (run_state != "parked" and run_state not in runs.MESSAGE_STATES):
        return None
    text = runs.clip(text, runs.MAX_INBOX_BYTES)
    await repository.answer(conn, decision_id, runs.clip(text, runs.MAX_ANSWER_BYTES))
    inbox_run = run_id
    if run_state == "parked":
        inbox_run = await resume_parked(conn, None, run_id, f"as its wait #{decision_id} woke", woken_by=text)
    await repository.to_inbox(conn, inbox_run, text, decision_id)
    await write_event(conn, inbox_run, {"text": text, "decision": {"id": decision_id, "category": "wait"}})
    return inbox_run


async def wake_waits(engine: AsyncEngine, batch: int) -> int:
    """Answer every open wait, at most ``batch``, whose event or time came, each in a transaction of its own; how many
    woke."""
    async with UnitOfWork(engine) as conn:
        waits = await repository.open_waits(conn, batch)
    woken = 0
    for row in waits:
        async with UnitOfWork(engine) as conn:
            event = None if row.wake_on is None else await repository.event_for(conn, row.project_id, row.wake_on)
            if event is None and not row.due:
                continue
            text = woke_message(row.id, row.question, time=row.wake_at, event=event)
            inbox_run = await wake(conn, row.id, row.project_id, row.plan_id, text)
        if inbox_run is not None:
            woken += 1
            log.info("wait woke", extra={"decision_id": row.id, "run_id": inbox_run, "event": bool(event)})
    return woken


async def resume_answered(engine: AsyncEngine, batch: int) -> int:
    """Resume every parked run, at most ``batch``, with no open decision and a message of its inbox no worker took
    (``run_waits`` says why), each in a transaction of its own under the locks an answer takes; how many resumed."""
    async with UnitOfWork(engine) as conn:
        parked = await repository.stranded_runs(conn, batch)
    resumed = 0
    for row in parked:
        async with UnitOfWork(engine) as conn:
            if row.plan_id is not None:
                await lock_plan(conn, row.project_id, row.plan_id)
            if not await repository.lock_stranded(conn, row.id):  # answered, resumed or ended meanwhile
                continue
            new_id = await resume_parked(conn, None, row.id, "as an answer it had not taken was in its inbox")
            await repository.move_untaken(conn, row.id, new_id)
        resumed += 1
        log.info("parked run resumed with its untaken answer", extra={"run_id": row.id, "new_run_id": new_id})
    return resumed
