"""The agent of a plan run waits for a time or an event of the hub (``evo-agents worker wait``) instead of asking its
owner to say when it came: POST /v1/worker/runs/{run}/waits, and the hub's own answer when it comes.

The route stores a decision of category ``wait`` (``runs.WAIT_DECISION_CATEGORY``) whose question is the agent's reason,
whose context says what the run waits for, and whose options let the person asked cut it short: Go on now and Stop
waiting. It notifies nobody, since it asks nobody anything, but it is open like any decision: the daemon moves the run
to waiting at the end of the agent's turn, and the reaper parks it at its next pass, since nobody answers it, so the
wait holds no worker (``run_state.park_waiting``); a wait that wakes within ``runs.DECISION_WAIT_SECONDS`` keeps the
slot as a decision does that long. ``wake_at`` is when it wakes at the latest: ``until``, or MAX_WAIT_DAYS from now when
only ``on`` is given, and never later than that, so a parked run is still there (``run_state.expire_parked`` leaves a
run alone while a wait of it has not come). ``wake_on`` is the event that wakes it sooner: an event of the run's project
in hub_events of kind ``on`` (``hub_events.KINDS``, or a kind a core plugin records of its own) whose payload contains
``contains``, written after the wait was asked (``after`` is the project's last event then). A wait on another plan's
step names ``plan.step`` (``evo_agents.hub.plan_step_status``) with the plan, the step and the status, such as
``{"plan": "northwind-launch", "step": "8", "status": "done"}``; the payload holds a plan's id and a step as text, so a
number given for one of TEXT_KEYS is taken as its text (``--contains step=8`` reads 8 as JSON).

``wake_waits``, on each pass of the reaper (``run_state.recover_runs``), answers every open wait whose event or time
came, each in a transaction of its own under the locks an answer takes (``decisions.lock_run_of``): the decision is
answered by nobody (``answered_by`` NULL), its ``answer_text`` the words the agent gets, and those go to the run's inbox
as a message naming the decision, as an owner's answer does; a parked run is resumed first, by the reaper
(``decisions.resume_parked``). A wait of a run that ended was cancelled with the run's other decisions.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, runs, tables
from evo_agents.hub.server.decisions import (
    Decision,
    DecisionAsk,
    OptionIn,
    lock_run_of,
    resume_parked,
    store_decision,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import one_line
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import LINE, REFUSALS, RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_REASON_CHARS = 1000
MAX_CONTAINS_BYTES = 2048  # what a wait's event must contain, as JSON
PAYLOAD_IN_MESSAGE = 2048  # bytes of the event's payload the message that wakes the agent repeats
MAX_WAIT = timedelta(days=runs.MAX_WAIT_DAYS)
TEXT_KEYS = {hub_events.PLAN_STEP: ("plan", "plan_id", "step")}  # keys of the payload of a kind that hold text
GO_ON, STOP = "now", "stop"
OPTIONS = (
    (GO_ON, "Go on now", "What the run waits for came, or it need not wait: the agent goes on at once."),
    (STOP, "Stop waiting", "What the run waits for will not come: the agent goes on without it."),
)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class WaitIn(BaseModel):
    reason: str = Field(
        min_length=1, max_length=MAX_REASON_CHARS, description="what the run waits for and why, for the owner"
    )
    until: datetime | None = Field(
        None, description=f"when to wake at the latest, with a timezone, at most {runs.MAX_WAIT_DAYS} days ahead"
    )
    on: str | None = Field(
        None, min_length=1, max_length=120, description="a kind of hub event to wake on sooner, such as canvas.baseline"
    )
    contains: dict[str, Any] = Field(
        default_factory=dict, description="what the event's payload contains, as JSONB containment reads it"
    )
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )

    @model_validator(mode="after")
    def _consistent(self):
        if not self.reason.strip():
            raise ValueError("a wait needs a reason")
        if self.until is None and self.on is None:
            raise ValueError("a wait wakes at a time (until), on an event of the hub (on), or both")
        if self.until is not None and self.until.utcoffset() is None:
            raise ValueError("until needs a timezone, such as 2026-10-30T09:00:00Z")
        if self.on is None and self.contains:
            raise ValueError("contains filters the event of on: name the kind with on")
        if self.on is not None and self.on not in hub_events.KINDS and not hub_events.OWN_KIND.fullmatch(self.on):
            kinds = ", ".join(hub_events.KINDS)
            raise ValueError(f"on is a kind of hub event: one of {kinds}, or plugin.<plugin>.<name>")
        for key in TEXT_KEYS.get(self.on, ()):
            value = self.contains.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                self.contains[key] = str(value)
        if len(json.dumps(self.contains, ensure_ascii=False).encode()) > MAX_CONTAINS_BYTES:
            raise ValueError(f"contains takes at most {MAX_CONTAINS_BYTES} bytes of JSON")
        return self


class WaitAsk(DecisionAsk):
    """The decision a wait opens: DecisionAsk, in the category the agent cannot name with `worker ask`."""

    category: Literal["wait"]  # runs.WAIT_DECISION_CATEGORY


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


@worker_router.post(
    "/runs/{run_id}/waits",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def wait(request: Request, run_id: RunId, body: WaitIn, user: CurrentUser) -> Decision:
    """Wait, in a plan run this worker holds, for a time or an event of the hub: a decision of category wait the hub
    answers when either comes."""
    wake_at = wake_time(body, datetime.now(UTC))
    e = tables.hub_events
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
        wake: dict = {"wake_at": wake_at, "wake_on": None}
        if body.on is not None:
            last = select(func.coalesce(func.max(e.c.id), 0)).where(e.c.project_id == row.project_id)
            after = (await conn.execute(last)).scalar_one()
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
        f"It waited for: {one_line(reason, 500)}",
        "Check on the hub that what you waited for is there before you rely on it, and wait again with `evo-agents "
        "worker wait` when it is not.",
    ]
    return runs.clip("\n".join(lines), runs.MAX_ANSWER_BYTES)


async def _event_for(conn: AsyncConnection, project_id: int, wake_on: dict):
    """The first event of project ``project_id`` a wait of ``wake_on`` wakes on, None while none came."""
    e = tables.hub_events
    query = (
        select(e.c.id, e.c.kind, e.c.payload, e.c.created_at)
        .where(
            e.c.project_id == project_id,
            e.c.id > int(wake_on.get("after") or 0),
            e.c.kind == wake_on["kind"],
            e.c.payload.contains(wake_on.get("contains") or {}),
        )
        .order_by(e.c.id)
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


async def wake(conn: AsyncConnection, decision_id: int, project_id: int, plan_id: str | None, text: str) -> int | None:
    """Answer wait ``decision_id`` as the hub with ``text``, in the caller's transaction, under the locks an answer
    takes: the message goes to its run's inbox, or to the inbox of the run that resumes it when it is parked; that
    run's id, or None when the wait is no longer open or its run takes no answer."""
    d, r, i = tables.decisions, tables.runs, tables.run_inbox
    run_id, run_state, _ = await lock_run_of(conn, project_id, plan_id, decision_id)
    state = (await conn.execute(select(d.c.state).where(d.c.id == decision_id).with_for_update())).scalar_one()
    if state != "open" or (run_state != "parked" and run_state not in runs.MESSAGE_STATES):
        return None
    await conn.execute(
        update(d).values(state="answered", answer_text=text, answered_at=func.now()).where(d.c.id == decision_id)
    )
    inbox_run = run_id
    if run_state == "parked":
        inbox_run = await resume_parked(conn, None, run_id, f"as its wait #{decision_id} woke", woken_by=text)
    owner = (await conn.execute(select(r.c.dispatched_by).where(r.c.id == inbox_run))).scalar_one()
    await conn.execute(insert(i).values(run_id=inbox_run, sent_by=owner, body=text, decision_id=decision_id))
    await write_event(conn, inbox_run, {"text": text, "decision": {"id": decision_id, "category": "wait"}})
    return inbox_run


async def wake_waits(engine: AsyncEngine, batch: int) -> int:
    """Answer every open wait, at most ``batch``, whose event or time came, each in a transaction of its own; how many
    woke."""
    d = tables.decisions
    due = (d.c.wake_at <= func.now()).label("due")
    query = (
        select(d.c.id, d.c.project_id, d.c.plan_id, d.c.question, d.c.wake_at, d.c.wake_on, due)
        .where(d.c.category == runs.WAIT_DECISION_CATEGORY, d.c.state == "open")
        .order_by(d.c.wake_at, d.c.id)
        .limit(batch)
    )
    async with engine.begin() as conn:
        waits = (await conn.execute(query)).all()
    woken = 0
    for row in waits:
        async with engine.begin() as conn:
            event = None if row.wake_on is None else await _event_for(conn, row.project_id, row.wake_on)
            if event is None and not row.due:
                continue
            text = woke_message(row.id, row.question, time=row.wake_at, event=event)
            inbox_run = await wake(conn, row.id, row.project_id, row.plan_id, text)
        if inbox_run is not None:
            woken += 1
            log.info("wait woke", extra={"decision_id": row.id, "run_id": inbox_run, "event": bool(event)})
    return woken
