"""What the Orchestrator of a project's default system does in an ops thread (``ops_threads``: the thread the hub opens
for a batch of the ops queue), beyond answer, reassign and ask-owner of ``orchestrator_actions``, whose frame it acts
in: a chat run of the thread's Orchestrator (404 for a run in no ops thread, 403 for another agent), while the hub has
not settled the thread's batch (409). An Orchestrator of a system that is not the default one has no ops thread, so it
keeps only the agents it orchestrates.

Each action posts a ``Decision:`` line in the thread, in the Orchestrator's name, writes the audit log, and marks the
items of the batch it handled ``handled_by`` orchestrator. The routes take the worker token of the worker that holds
the chat run; the hub runtime's tools call the same code (``hub_tools``):

- POST /v1/worker/runs/{id}/ops/rerun: hand the work of a chat or task run of the batch that failed or was lost again,
  for a new session, to any worker that fits (``ops_rules.hand_again``).
- POST /v1/worker/runs/{id}/ops/redeliver: the same, to a healthy worker of the project (``ops_rules.healthy_worker``);
  409 when none is.
- POST /v1/worker/runs/{id}/ops/answer-wait: answer a stuck wait of the batch (a decision of category wait, never a
  decision a person must make) with a text, which wakes its run (``run_waits.wake``).
- POST /v1/worker/runs/{id}/ops/curator-resume: let the Curator's schedules of the project run again that its breaker
  paused (never those a person paused), as ``evo-agents hub curator resume`` does.
- POST /v1/worker/runs/{id}/ops/read: mark a notice of the project read.
- POST /v1/worker/runs/{id}/ops/finding: write a finding for the Curator, under the project's default label, with the
  batch as its evidence.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, review, runs, tables, triage_rules
from evo_agents.hub.server import audit, ops_rules, orchestrator_actions, run_waits
from evo_agents.hub.server.chat_agents import REFUSALS, AgentRun, agent_run
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.orchestrator_actions import Frame, Reason
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

CURATOR_RESUME = "curator.resume"  # curator.RESUME, the action of `evo-agents hub curator resume`
Id = Annotated[int, Field(ge=1, le=2**63 - 1)]


class OpsRunIn(BaseModel):
    run_id: Id = Field(description="a chat or task run of the batch that failed or was lost")
    reason: Reason


class OpsWaitIn(BaseModel):
    decision_id: Id = Field(description="a stuck wait of the batch: a decision of category wait")
    text: str = Field(min_length=1, max_length=runs.MAX_ANSWER_BYTES, description="the answer that wakes its run")
    reason: Reason


class OpsReasonIn(BaseModel):
    reason: Reason


class OpsReadIn(BaseModel):
    notification_id: Id = Field(description="a notice of the project")
    reason: Reason


class OpsFindingIn(BaseModel):
    lens: review.Lens
    severity: review.Severity
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000, description="what is wrong with the system, and the fix")
    reason: Reason


class OpsActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the Orchestrator's name")
    run_id: int | None = Field(None, description="the run the work went to, or the run the answer woke")
    finding_id: int | None = Field(None, description="the finding it wrote")
    handled: int = Field(0, description="items of the batch it marked handled")


async def ops_frame(conn: AsyncConnection, run: AgentRun):
    """(the frame, the thread's batch) of Orchestrator run ``run`` in an ops thread; 404 for a run in another thread."""
    frame = await orchestrator_actions.frame_of(conn, run)
    if frame.thread.rule != triage_rules.OPS:
        raise HTTPException(404, f"chat run {run.run_id} is in no ops thread: the ops actions are the ops threads'")
    b = tables.ops_batches
    batch = (await conn.execute(select(b).where(b.c.thread_id == frame.thread.id).with_for_update())).one()
    return frame, batch


async def handled(conn: AsyncConnection, batch_id: int, **where) -> int:
    """Mark the pending items of batch ``batch_id`` whose columns equal ``where`` handled by the Orchestrator."""
    i = tables.ops_items
    query = update(i).values(state="handled", handled_by=ops_rules.ORCHESTRATOR, handled_at=func.now())
    query = query.where(i.c.batch_id == batch_id, i.c.state == "pending")
    for column, value in where.items():
        query = query.where(i.c[column] == value)
    return (await conn.execute(query)).rowcount or 0


async def _item(conn: AsyncConnection, batch_id: int, column: str, value: int) -> None:
    i = tables.ops_items
    query = select(i.c.id).where(i.c.batch_id == batch_id, i.c[column] == value).limit(1)
    if (await conn.execute(query)).first() is None:
        raise HTTPException(404, f"{column.removesuffix('_id')} {value} is no item of this thread's batch")


async def _done(conn: AsyncConnection, frame: Frame, action: str, line: str, tail: str) -> int:
    message_id = await orchestrator_actions.said(conn, frame, f"Decision: {line}")
    await orchestrator_actions.audited(conn, frame, action, tail)
    log.info("ops action", extra={"action": action, "thread_id": frame.thread.id, "run_id": frame.run.run_id})
    return message_id


async def rerun(conn: AsyncConnection, run: AgentRun, body: OpsRunIn, *, redeliver: bool = False) -> OpsActed:
    """Hand the work of run ``body.run_id`` of the batch again, to a healthy worker when ``redeliver``."""
    frame, batch = await ops_frame(conn, run)
    await _item(conn, batch.id, "run_id", body.run_id)
    failed = await ops_rules.failed_run(conn, body.run_id)
    if failed is None:
        raise HTTPException(409, f"run {body.run_id} is no chat or task run that failed or was lost")
    worker_id = None
    if redeliver:
        worker_id = await ops_rules.healthy_worker(conn, batch.project_id, failed)
        if worker_id is None:
            raise HTTPException(409, f"no healthy worker of the project fits run {body.run_id}: rerun it, or wait")
    new_id = await ops_rules.hand_again(conn, failed, worker_id)
    if new_id is None:
        raise HTTPException(409, f"the work of run {body.run_id} went to no run")
    count = await handled(conn, batch.id, run_id=body.run_id)
    verb = "redelivered" if redeliver else "ran again"
    line = f"{verb} the work of run #{body.run_id} as run #{new_id}: {body.reason}"
    action = audit.OPS_REDELIVER if redeliver else audit.OPS_RERUN
    message_id = await _done(conn, frame, action, line, f" run:{body.run_id} next:{new_id}")
    return OpsActed(message_id=message_id, run_id=new_id, handled=count)


async def answer_wait(conn: AsyncConnection, run: AgentRun, body: OpsWaitIn) -> OpsActed:
    """Answer the stuck wait ``body.decision_id`` of the batch, which wakes its run."""
    frame, batch = await ops_frame(conn, run)
    await _item(conn, batch.id, "decision_id", body.decision_id)
    d = tables.decisions
    query = select(d.c.category, d.c.state, d.c.project_id, d.c.plan_id).where(d.c.id == body.decision_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.project_id != batch.project_id:
        raise HTTPException(404, f"decision {body.decision_id} is no decision of the project")
    if row.category != runs.WAIT_DECISION_CATEGORY:
        raise HTTPException(409, f"decision {body.decision_id} is the owner's to make: ask the owner, never answer it")
    woke = (
        None
        if row.state != "open"
        else await run_waits.wake(
            conn, body.decision_id, row.project_id, row.plan_id, runs.clip(body.text, runs.MAX_ANSWER_BYTES)
        )
    )
    if woke is None:
        raise HTTPException(409, f"wait {body.decision_id} is answered already, or its run takes no answer")
    count = await handled(conn, batch.id, decision_id=body.decision_id)
    line = f"answered the wait #{body.decision_id}, which woke run #{woke}: {body.reason}"
    message_id = await _done(conn, frame, audit.OPS_ANSWER_WAIT, line, f" decision:{body.decision_id} run:{woke}")
    return OpsActed(message_id=message_id, run_id=woke, handled=count)


async def curator_resume(conn: AsyncConnection, run: AgentRun, body: OpsReasonIn) -> OpsActed:
    """Let the schedules of the project's Curator that its breaker paused run again."""
    frame, batch = await ops_frame(conn, run)
    s, p = tables.schedules, tables.projects
    resumed = (
        update(s)
        .values(paused_at=None, paused_by=None, pause_reason=None, updated_at=func.now())
        .where(s.c.project_id == batch.project_id, s.c.paused_at.is_not(None), s.c.paused_by.is_(None))
    )
    count = (await conn.execute(resumed)).rowcount or 0
    if not count:
        raise HTTPException(
            409, "no schedule of the project's Curator is paused by its breaker: a person's pause stays"
        )
    name = (await conn.execute(select(p.c.name).where(p.c.id == batch.project_id))).scalar_one()
    await audit.record(
        conn,
        actor_id=run.principal.user_id,
        token_id=run.principal.token_id,
        action=CURATOR_RESUME,
        target=name,
        project_id=batch.project_id,
    )
    items = await handled(conn, batch.id, kind=hub_events.CURATOR_PAUSED)
    line = f"resumed {count} schedules of the Curator its breaker paused: {body.reason}"
    message_id = await _done(conn, frame, audit.OPS_CURATOR_RESUME, line, f" schedules:{count}")
    return OpsActed(message_id=message_id, handled=items)


async def read(conn: AsyncConnection, run: AgentRun, body: OpsReadIn) -> OpsActed:
    """Mark notice ``body.notification_id`` of the project read."""
    frame, batch = await ops_frame(conn, run)
    n = tables.notifications
    query = (
        update(n)
        .values(read_at=func.coalesce(n.c.read_at, func.now()))
        .where(n.c.id == body.notification_id, n.c.project_id == batch.project_id, n.c.kind == "notice")
    )
    if not (await conn.execute(query)).rowcount:
        raise HTTPException(404, f"notification {body.notification_id} is no notice of the project")
    line = f"marked notice #{body.notification_id} read: {body.reason}"
    message_id = await _done(conn, frame, audit.OPS_READ, line, f" notification:{body.notification_id}")
    return OpsActed(message_id=message_id)


async def finding(conn: AsyncConnection, run: AgentRun, body: OpsFindingIn) -> OpsActed:
    """Write a finding for the Curator, as the Orchestrator's run, with the batch as its evidence."""
    frame, batch = await ops_frame(conn, run)
    p, f, i = tables.projects, tables.findings, tables.ops_items
    label = (await conn.execute(select(p.c.default_label).where(p.c.id == batch.project_id))).scalar_one()
    items = select(i.c.run_id, i.c.decision_id, i.c.cause).where(i.c.batch_id == batch.id).order_by(i.c.id).limit(20)
    evidence = [{"batch_id": batch.id, "key": batch.key, "thread_id": frame.thread.id}]
    evidence += [
        {key: value for key, value in row._mapping.items() if value is not None} for row in await conn.execute(items)
    ]
    values = {
        "project_id": batch.project_id,
        "run_id": run.run_id,
        "lens": body.lens,
        "severity": body.severity,
        "title": body.title,
        "body": body.body,
        "evidence": evidence,
        "label": label,
    }
    finding_id = (await conn.execute(insert(f).values(**values).returning(f.c.id))).scalar_one()
    line = f"wrote finding #{finding_id} for the Curator ({body.lens}, {body.severity}): {body.title}. {body.reason}"
    message_id = await _done(conn, frame, audit.OPS_FINDING, line, f" finding:{finding_id}")
    return OpsActed(message_id=message_id, finding_id=finding_id)


async def held(conn: AsyncConnection, user, run_id: int) -> AgentRun:
    """The chat run ``run_id`` of the worker of ``user``, its row locked."""
    run = await agent_run(conn, user, run_id)
    await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
    return run


_RESPONSES = {403: {"model": ErrorBody}, **REFUSALS}


@worker_router.post("/runs/{run_id}/ops/rerun", response_model=OpsActed, responses=_RESPONSES)
async def rerun_route(request: Request, run_id: RunId, body: OpsRunIn, user: CurrentUser) -> OpsActed:
    """Run the work of a failed run of this ops thread's batch again, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await rerun(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/redeliver", response_model=OpsActed, responses=_RESPONSES)
async def redeliver_route(request: Request, run_id: RunId, body: OpsRunIn, user: CurrentUser) -> OpsActed:
    """Hand the work of a failed run of this ops thread's batch to a healthy worker, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await rerun(conn, await held(conn, user, run_id), body, redeliver=True)


@worker_router.post("/runs/{run_id}/ops/answer-wait", response_model=OpsActed, responses=_RESPONSES)
async def answer_wait_route(request: Request, run_id: RunId, body: OpsWaitIn, user: CurrentUser) -> OpsActed:
    """Answer a stuck wait of this ops thread's batch, as its Orchestrator's chat run."""
    async with request.app.state.engine.begin() as conn:
        return await answer_wait(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/curator-resume", response_model=OpsActed, responses=_RESPONSES)
async def curator_resume_route(request: Request, run_id: RunId, body: OpsReasonIn, user: CurrentUser) -> OpsActed:
    """Resume the Curator's schedules its breaker paused, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await curator_resume(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/read", response_model=OpsActed, responses=_RESPONSES)
async def read_route(request: Request, run_id: RunId, body: OpsReadIn, user: CurrentUser) -> OpsActed:
    """Mark a notice of the project read, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await read(conn, await held(conn, user, run_id), body)


@worker_router.post("/runs/{run_id}/ops/finding", response_model=OpsActed, responses=_RESPONSES)
async def finding_route(request: Request, run_id: RunId, body: OpsFindingIn, user: CurrentUser) -> OpsActed:
    """Write a finding for the Curator, as the Orchestrator's chat run of an ops thread."""
    async with request.app.state.engine.begin() as conn:
        return await finding(conn, await held(conn, user, run_id), body)


__all__ = [
    "OpsActed",
    "OpsFindingIn",
    "OpsReadIn",
    "OpsReasonIn",
    "OpsRunIn",
    "OpsWaitIn",
    "answer_wait",
    "curator_resume",
    "finding",
    "handled",
    "ops_frame",
    "read",
    "rerun",
    "worker_router",
]
