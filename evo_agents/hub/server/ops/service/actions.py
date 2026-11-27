"""What the Orchestrator of a project's default system does in an ops thread (``ops.service.threads``: the thread the hub opens
for a batch of the ops queue), beyond answer, reassign and ask-owner of ``orchestrator_actions``, whose frame it acts
in: a chat run of the thread's Orchestrator (404 for a run in no ops thread, 403 for another agent), while the hub has
not settled the thread's batch (409). An Orchestrator of a system that is not the default one has no ops thread, so it
keeps only the agents it orchestrates.

Each action posts a ``Decision:`` line in the thread, in the Orchestrator's name, writes the audit log, and marks the
items of the batch it handled ``handled_by`` orchestrator. The routes (``ops.routes``) take the worker token of the worker that holds
the chat run; the hub runtime's tools call the same code (``hub_tools``):

- POST /v1/worker/runs/{id}/ops/rerun: hand the work of a chat or task run of the batch that failed or was lost again,
  for a new session, to any worker that fits (``ops.service.rules.hand_again``).
- POST /v1/worker/runs/{id}/ops/redeliver: the same, to a healthy worker of the project (``ops.service.rules.healthy_worker``);
  409 when none is.
- POST /v1/worker/runs/{id}/ops/answer-wait: answer a stuck wait of the batch (a decision of category wait, never a
  decision a person must make) with a text, which wakes its run (``run_waits.wake``).
- POST /v1/worker/runs/{id}/ops/curator-resume: let the Curator's schedules of the project run again that its breaker
  paused (never those a person paused), as ``evo-agents hub curator resume`` does.
- POST /v1/worker/runs/{id}/ops/read: mark a notice of the project read.
- POST /v1/worker/runs/{id}/ops/finding: write a finding for the Curator, under the project's default label, with the
  batch as its evidence.
- POST /v1/worker/runs/{id}/ops/priority: give a queued run of the project another priority (``runs.PRIORITIES``), the
  order a worker claims its queue in (``runs.service.priority.set_priority``); 409 for a run that is not queued. A
  ``run.queued_long`` item of the batch is the usual cause; a queued run of the project outside the batch is allowed.
- POST /v1/worker/runs/{id}/ops/plan-dispatch: hand a stalled plan of the batch (a ``plan.stalled`` item) again to the
  entry agent of the project's default system, as ``plan_systems`` does for a person: in the name
  of the system's owner, the person at the origin of the ops thread's work, with the same refusals (409 for a plan a
  trace still runs, an active plan run or step run, no pending step, a plan the Curator made).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, runs, triage_rules
from evo_agents.hub.server import audit, run_waits
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.service.agents import AgentRun, agent_run
from evo_agents.hub.server.ops.models.actions import (
    CURATOR_RESUME,
    OpsActed,
    OpsFindingIn,
    OpsPlanIn,
    OpsPriorityIn,
    OpsReadIn,
    OpsReasonIn,
    OpsRunIn,
    OpsWaitIn,
)
from evo_agents.hub.server.ops.repository import actions as store
from evo_agents.hub.server.ops.repository.rules import failed_run
from evo_agents.hub.server.ops.service import rules as ops_rules
from evo_agents.hub.server.orchestrator.service import actions as orchestrator_actions
from evo_agents.hub.server.orchestrator.service import threads as orchestrator
from evo_agents.hub.server.orchestrator.service.actions import Frame
from evo_agents.hub.server.plan_systems.service import dispatch_to_system
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.priority import set_priority

log = logging.getLogger(__name__)


async def ops_frame(conn: AsyncConnection, run: AgentRun):
    """(the frame, the thread's batch) of Orchestrator run ``run`` in an ops thread; 404 for a run in another thread."""
    frame = await orchestrator_actions.frame_of(conn, run)
    if frame.thread.rule != triage_rules.OPS:
        raise NotFound(f"chat run {run.run_id} is in no ops thread: the ops actions are the ops threads'")
    return frame, await store.batch_of_thread(conn, frame.thread.id)


async def item(conn: AsyncConnection, batch_id: int, column: str, value: int) -> None:
    """404 unless batch ``batch_id`` has an item whose ``column`` is ``value``."""
    if not await store.has_item(conn, batch_id, column, value):
        raise NotFound(f"{column.removesuffix('_id')} {value} is no item of this thread's batch")


async def done(conn: AsyncConnection, frame: Frame, action: str, line: str, tail: str) -> int:
    """Post ``line`` as the ``Decision:`` line of ``frame``'s thread and audit ``action``; the message's id."""
    message_id = await orchestrator_actions.said(conn, frame, f"Decision: {line}")
    await orchestrator_actions.audited(conn, frame, action, tail)
    log.info("ops action", extra={"action": action, "thread_id": frame.thread.id, "run_id": frame.run.run_id})
    return message_id


async def rerun(conn: AsyncConnection, run: AgentRun, body: OpsRunIn, *, redeliver: bool = False) -> OpsActed:
    """Hand the work of run ``body.run_id`` of the batch again, to a healthy worker when ``redeliver``."""
    frame, batch = await ops_frame(conn, run)
    await item(conn, batch.id, "run_id", body.run_id)
    failed = await failed_run(conn, body.run_id)
    if failed is None:
        raise Conflict(f"run {body.run_id} is no chat or task run that failed or was lost")
    worker_id = None
    if redeliver:
        worker_id = await ops_rules.healthy_worker(conn, batch.project_id, failed)
        if worker_id is None:
            raise Conflict(f"no healthy worker of the project fits run {body.run_id}: rerun it, or wait")
    new_id = await ops_rules.hand_again(conn, failed, worker_id)
    if new_id is None:
        raise Conflict(f"the work of run {body.run_id} went to no run")
    count = await store.mark_handled(conn, batch.id, run_id=body.run_id)
    verb = "redelivered" if redeliver else "ran again"
    line = f"{verb} the work of run #{body.run_id} as run #{new_id}: {body.reason}"
    action = audit.OPS_REDELIVER if redeliver else audit.OPS_RERUN
    message_id = await done(conn, frame, action, line, f" run:{body.run_id} next:{new_id}")
    return OpsActed(message_id=message_id, run_id=new_id, handled=count)  # pyright: ignore[reportCallIssue]


async def answer_wait(conn: AsyncConnection, run: AgentRun, body: OpsWaitIn) -> OpsActed:
    """Answer the stuck wait ``body.decision_id`` of the batch, which wakes its run."""
    frame, batch = await ops_frame(conn, run)
    await item(conn, batch.id, "decision_id", body.decision_id)
    row = await store.decision_row(conn, body.decision_id)
    if row is None or row.project_id != batch.project_id:
        raise NotFound(f"decision {body.decision_id} is no decision of the project")
    if row.category != runs.WAIT_DECISION_CATEGORY:
        raise Conflict(f"decision {body.decision_id} is the owner's to make: ask the owner, never answer it")
    woke = (
        None
        if row.state != "open"
        else await run_waits.wake(
            conn, body.decision_id, row.project_id, row.plan_id, runs.clip(body.text, runs.MAX_ANSWER_BYTES)
        )
    )
    if woke is None:
        raise Conflict(f"wait {body.decision_id} is answered already, or its run takes no answer")
    count = await store.mark_handled(conn, batch.id, decision_id=body.decision_id)
    line = f"answered the wait #{body.decision_id}, which woke run #{woke}: {body.reason}"
    message_id = await done(conn, frame, audit.OPS_ANSWER_WAIT, line, f" decision:{body.decision_id} run:{woke}")
    return OpsActed(message_id=message_id, run_id=woke, handled=count)  # pyright: ignore[reportCallIssue]


async def curator_resume(conn: AsyncConnection, run: AgentRun, body: OpsReasonIn) -> OpsActed:
    """Let the schedules of the project's Curator that its breaker paused run again."""
    frame, batch = await ops_frame(conn, run)
    count = await store.resume_schedules(conn, batch.project_id)
    if not count:
        raise Conflict("no schedule of the project's Curator is paused by its breaker: a person's pause stays")
    name = await store.project_name(conn, batch.project_id)
    await Auditor(conn, batch.project_id, run.principal).record(CURATOR_RESUME, name)
    items = await store.mark_handled(conn, batch.id, kind=hub_events.CURATOR_PAUSED)
    line = f"resumed {count} schedules of the Curator its breaker paused: {body.reason}"
    message_id = await done(conn, frame, audit.OPS_CURATOR_RESUME, line, f" schedules:{count}")
    return OpsActed(message_id=message_id, handled=items)  # pyright: ignore[reportCallIssue]


async def read(conn: AsyncConnection, run: AgentRun, body: OpsReadIn) -> OpsActed:
    """Mark notice ``body.notification_id`` of the project read."""
    frame, batch = await ops_frame(conn, run)
    if not await store.mark_notice_read(conn, body.notification_id, batch.project_id):
        raise NotFound(f"notification {body.notification_id} is no notice of the project")
    line = f"marked notice #{body.notification_id} read: {body.reason}"
    message_id = await done(conn, frame, audit.OPS_READ, line, f" notification:{body.notification_id}")
    return OpsActed(message_id=message_id)  # pyright: ignore[reportCallIssue]


async def finding(conn: AsyncConnection, run: AgentRun, body: OpsFindingIn) -> OpsActed:
    """Write a finding for the Curator, as the Orchestrator's run, with the batch as its evidence."""
    frame, batch = await ops_frame(conn, run)
    label = await store.default_label(conn, batch.project_id)
    evidence = [{"batch_id": batch.id, "key": batch.key, "thread_id": frame.thread.id}]
    evidence += await store.batch_evidence(conn, batch.id)
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
    finding_id = await store.insert_finding(conn, values)
    line = f"wrote finding #{finding_id} for the Curator ({body.lens}, {body.severity}): {body.title}. {body.reason}"
    message_id = await done(conn, frame, audit.OPS_FINDING, line, f" finding:{finding_id}")
    return OpsActed(message_id=message_id, finding_id=finding_id)  # pyright: ignore[reportCallIssue]


async def priority(conn: AsyncConnection, run: AgentRun, body: OpsPriorityIn) -> OpsActed:
    """Give queued run ``body.run_id`` of the project the priority ``body.level``."""
    frame, batch = await ops_frame(conn, run)
    if not await store.run_in_project(conn, body.run_id, batch.project_id):
        raise NotFound(f"run {body.run_id} is no run of the project")
    was = await set_priority(conn, body.run_id, body.level, f"@{run.profile.handle}, the Orchestrator,")
    count = await store.mark_handled(conn, batch.id, run_id=body.run_id)
    line = f"set the priority of queued run #{body.run_id} to {body.level}, from {was}: {body.reason}"
    message_id = await done(conn, frame, audit.OPS_PRIORITY, line, f" run:{body.run_id} priority:{was}->{body.level}")
    return OpsActed(message_id=message_id, run_id=body.run_id, handled=count)  # pyright: ignore[reportCallIssue]


async def plan_dispatch(
    conn: AsyncConnection, run: AgentRun, body: OpsPlanIn, *, public_url: str | None = None
) -> OpsActed:
    """Hand stalled plan ``body.plan_id`` of the batch again to the entry agent of the project's default system."""
    frame, batch = await ops_frame(conn, run)
    subject = f"plan:{body.plan_id}"
    if not await store.has_stalled(conn, batch.id, subject):
        raise NotFound(f"plan {body.plan_id} is no stalled plan of this thread's batch")
    project = await store.project_name(conn, batch.project_id)
    slug = await store.system_slug(conn, frame.thread.system_id)
    owner = await orchestrator.machine_person(conn, run.principal.user_id)
    sent = await dispatch_to_system(conn, owner, project, body.plan_id, slug, public_url)
    count = await store.mark_handled(conn, batch.id, kind=hub_events.PLAN_STALLED, subject=subject)
    line = (
        f"handed plan {body.plan_id} (revision {sent.plan_revision}) again to the entry agent of system {slug}, "
        f"run #{sent.run_id} in trace #{sent.trace_id}: {body.reason}"
    )
    tail = f" plan:{body.plan_id} run:{sent.run_id} trace:{sent.trace_id}"
    message_id = await done(conn, frame, audit.OPS_PLAN_DISPATCH, line, tail)
    return OpsActed(message_id=message_id, run_id=sent.run_id, handled=count)  # pyright: ignore[reportCallIssue]


async def held(conn: AsyncConnection, user, run_id: int) -> AgentRun:
    """The chat run ``run_id`` of the worker of ``user``, its row locked."""
    run = await agent_run(conn, user, run_id)
    await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
    return run


async def act(
    engine: AsyncEngine, user, run_id: int, action: Callable[..., Awaitable[OpsActed]], body, **options
) -> OpsActed:
    """``action`` of the chat run ``run_id`` ``held`` locks, with ``body``, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await action(conn, await held(conn, user, run_id), body, **options)
