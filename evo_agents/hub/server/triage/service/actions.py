"""What the agent of a triage thread does on its own, behind ``evo-agents worker triage rerun|plan``
(``evo_agents.hub.triage_rules`` is the frame; ``docs/workers.md``, Failure triage, is the protocol).

The routes (``triage.routes``) take the worker token of the worker that holds a chat run (404 for another), whose thread is one of a
failed run's triage (``run_triage``; 404 for any other chat run) and whose dispatcher is that run's owner (403 for
another member, who may talk to the agent but not rerun the owner's run). They act as the owner, as the chain of the
chat run does, and the hub checks the frame again on each call, so an agent that ignored its instructions is refused:

- POST /v1/worker/runs/{id}/triage/rerun: run the failed step again (``controls.rerun_in``), on its agent's pool, with a
  runtime or account the owner names, for the causes of ``triage_rules.RERUN_CAUSES``, within the chain cap; a plan run
  only for a program it lacked or a 5xx of the hub, ``triage_rules.PLAN_RERUN`` (``controls.rerun_plan_in``). Nobody picks the machine. The rerun never runs as its owner (``run_access``), whatever the failed run did: only the owner lets a
  run act as them.
- POST /v1/worker/runs/{id}/triage/invite: invite the agent of another project, one the failed run's error names, into
  the thread's chat, so a mention can hand it work (a chat's mention limits, the hop of 3, stay). It is the group chat's
  own rule that decides (``chats.service.members.put_agent``): the owner needs the writer role there, and a label of the chat that
  someone in it does not clear is the 409 of the hub. A refusal is said in the thread, in the agent's name, and invites
  no one; the call answers 409 with the same reason.
- POST /v1/worker/runs/{id}/triage/cancel: in the thread of a stale run (cause ``triage.models.threads.STALE_CAUSE``, opened
  by ``triage.service.threads.open_stale``), ask the run's worker to stop it, as the owner's cancel of a held run does; ``rerun`` then
  runs it again once it has ended, for the cause ``stale``. A stale run's thread is the one whose run may be held or
  cancelled; every other thread's run is failed.
- POST /v1/worker/runs/{id}/triage/plan: change the ``verify`` or narrow the ``what`` of a pending step of the failed
  run's plan, or of several at once (``steps``), as one new revision under the owner's name (``plans.write_plan``) and
  one ``Decision:`` line, only as ``triage_rules.edit_problems`` allows; with ``rerun`` the rerun follows in the same
  transaction, so a refused rerun leaves the plan as it was.

Each success posts a line that starts with ``Decision:`` in the thread, in the agent's name, and writes an audit row
(``run.triage_rerun``, ``run.triage_plan_edit``, ``run.triage_cancel``) that names the failed run, the agent and the chat run. Nothing here
writes ``acceptance``, ``goal``, ``non_goals`` or a step that is not pending: ``edit_problems`` compares the whole plan.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, triage_rules
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import audit, orchestrator
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.chats.service import agents as chat_agents
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.agents import AgentRun
from evo_agents.hub.server.chats.service.members import put_agent
from evo_agents.hub.server.chats.service.runs import agent_access, chat_run, post_agent
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import Rerun
from evo_agents.hub.server.runs.service.candidates import candidates_of
from evo_agents.hub.server.runs.service.controls import rerun_in, rerun_plan_in
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, refuse_curator_plan
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.triage.models.actions import (
    OLD_VALUE_CHARS,
    OLD_VALUES_CHARS,
    RerunOptions,
    StepEdit,
    TriageActed,
    TriageCancelIn,
    TriageCancelled,
    TriageInvited,
    TriageInviteIn,
    TriagePlanIn,
    TriageRerunIn,
    decision,
    old_values,
    repos,
)
from evo_agents.hub.server.triage.models.threads import STALE_CAUSE
from evo_agents.hub.server.triage.repository import actions as store
from evo_agents.hub.server.triage.service.facts import chain_of


@dataclass(frozen=True)
class Frame:
    """What a triage call acts on: the chat run, the thread, the failed run, its owner and the owner's access."""

    agent: AgentRun
    chat_run_id: int
    chat_id: int
    root_id: int
    triage_id: int
    inferred: bool
    auto_reruns: int
    failed: Any  # the row of the failed run
    project: str
    owner: Principal
    access: ProjectAccess
    cause: str | None = None  # the failed run's cause; ``stale`` for a stale run's thread until the run failed
    stale: bool = False  # the thread was opened for a stale run (``triage.service.threads.open_stale``)
    on_worker: int | None = None  # the worker the chat run is on: where the agent installed what the run lacks


async def frame_of(conn: AsyncConnection, user: Principal, run_id: int) -> Frame:
    """The frame of chat run ``run_id`` of the worker of ``user``, checked as the module's docstring says."""
    agent = await chat_agents.agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if agent.kind == "chat" else None
    if found is None:
        raise NotFound(f"run {run_id} is a {agent.kind} run: only the agent of a chat run triages a failure")
    thread = await store.thread_at_root(conn, found.chat_id, found.root_id)
    if thread is None:
        raise NotFound(f"chat run {run_id} is in no thread the hub opened for a failed run")
    failed = await store.failed_row(conn, thread.run_id)
    if failed.project_id != agent.project_id:
        raise Conflict(f"run {failed.id} is in another project than the agent of chat run {run_id}")
    led = await orchestrator.thread_at(conn, found.chat_id, found.root_id) is not None  # its system's Orchestrator's
    if not failed.triage_enabled and not led:
        raise Conflict(f"triage is off for project {failed.project}: ask the owner in the thread")
    stale = thread.cause == STALE_CAUSE
    if failed.state != "failed" and not (stale and failed.state in (*runs.HELD_STATES, "cancelled")):
        raise Conflict(f"run {failed.id} is {failed.state} now, not failed: there is nothing to triage")
    if failed.dispatched_by != agent.principal.user_id:
        raise Forbidden(
            f"run {failed.id} belongs to another member than {agent.principal.login}, who dispatched this chat run: "
            "the agent acts on a run only for its owner",
        )
    owner = agent.principal
    access = await project_access(conn, owner, failed.project)
    on_worker = await store.worker_of(conn, run_id)
    return Frame(
        agent,
        run_id,
        found.chat_id,
        found.root_id,
        thread.id,
        thread.cause_inferred,
        thread.auto_reruns,
        failed,
        failed.project,
        owner,
        access,
        cause=failed.failure_cause if failed.state == "failed" or not stale else STALE_CAUSE,
        stale=stale,
        on_worker=on_worker,
    )


async def said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The line of the thread, in the agent's name; it opens with ``Decision:``."""
    chat = await agent_access(conn, frame.chat_id, frame.agent.profile, frame.owner.user_id)
    if chat is None:
        raise Conflict(f"@{frame.agent.profile.handle} is not in chat {frame.chat_id} any more")
    message_id, _ = await post_agent(
        conn, chat, frame.agent.profile, frame.chat_run_id, runs.clip(text, 4000), frame.root_id
    )
    return message_id


async def audited(conn: AsyncConnection, frame: Frame, action: str, tail: str) -> None:
    """Audit ``action`` of the agent in ``frame`` as its owner, naming the failed run, the agent and the chat run."""
    failed = frame.failed
    where = f"{frame.project}/{failed.plan_id}" if failed.plan_id else f"{frame.project}/author"
    where += f"#{failed.step_key}" if failed.step_key else ""
    target = f"{where} run:{failed.id} agent:@{frame.agent.profile.handle} triage_run:{frame.chat_run_id}{tail}"
    await Auditor(conn, frame.access.project_id, frame.owner).record(action, target)


async def rerun_failed(conn: AsyncConnection, frame: Frame, options: RerunOptions, *, edited: bool) -> int:
    """Run the failed run again as ``options`` say, once the frame allows it; the id of the new run."""
    failed = frame.failed
    plan_run = failed.kind == "plan" and frame.cause in triage_rules.PLAN_RERUN
    if failed.kind != "step" and not plan_run:
        raise Unprocessable(
            f"run {failed.id} is a {failed.kind} run: the agent reruns a step run only; ask the owner to dispatch the "
            f"{'plan' if failed.kind == 'plan' else 'author run'} again",
        )
    if failed.state in runs.HELD_STATES:
        raise Conflict(
            f"run {failed.id} is still {failed.state}: ask its worker to stop it with `evo-agents worker triage cancel`, "
            "and run it again once it has ended",
        )
    if frame.auto_reruns >= 1:
        raise Conflict(f"run {failed.id} was rerun by the agent once already: ask the owner")
    problem = triage_rules.rerun_problem(
        frame.cause, inferred=frame.inferred, confirmed=options.cause_confirmed, edited=edited
    )
    if problem:
        raise Unprocessable(problem)
    depth, causes = await chain_of(conn, failed.id)
    refusal = triage_rules.chain_problem(depth, causes, frame.cause)
    if refusal:
        raise Conflict(refusal)
    # the agent of the triage reruns the step as a run of the owner's that does not run as them: only the owner lets
    # a run act as them (``run_access``), never an agent on their behalf
    if plan_run:
        if options.account is not None or options.runtime is not None:
            raise Unprocessable(f"plan run {failed.id} goes on with the account and the runtime it asked for")
        new_id = await rerun_plan_in(conn, frame.owner, frame.project, failed.id)
    else:
        body = Rerun(account=options.account, as_me=False)  # pyright: ignore[reportCallIssue]
        view = await rerun_in(conn, frame.owner, frame.project, failed.id, body, runtime=options.runtime)
        new_id = view.id
    await store.count_rerun(conn, frame.triage_id)
    return new_id


async def rerun(conn: AsyncConnection, user: Principal, run_id: int, body: TriageRerunIn) -> tuple[int, TriageActed]:
    """Run the failed run of the thread of chat run ``run_id`` again for its owner, inside the frame: (the failed run's
    id, the answer)."""
    frame = await frame_of(conn, user, run_id)
    new_id = await rerun_failed(conn, frame, body, edited=False)
    text = (
        f"Decision: ran run #{frame.failed.id} again as run #{new_id} "
        f"(cause {frame.cause}): {body.reason} Undo: cancel run #{new_id}."
    )
    message_id = await said(conn, frame, text)
    await audited(conn, frame, audit.RUN_TRIAGE_RERUN, f" rerun:{new_id}")
    return frame.failed.id, TriageActed(message_id=message_id, run_id=new_id, plan_revision=None)


async def cancel(conn: AsyncConnection, user: Principal, run_id: int, body: TriageCancelIn) -> TriageCancelled:
    """Ask the worker of the stale run of the thread of chat run ``run_id`` to stop it, for its owner."""
    frame = await frame_of(conn, user, run_id)
    failed = frame.failed
    if not frame.stale:
        raise Conflict(f"run {failed.id} failed, which is final: only a stale run's thread stops a run")
    if failed.state not in runs.HELD_STATES:
        raise Conflict(f"run {failed.id} is {failed.state} now: its worker holds it no more")
    if not await store.ask_cancel(conn, failed.id):
        raise Conflict(f"the worker of run {failed.id} was asked to stop it already")
    text = (
        f"Decision: asked the worker of run #{failed.id} to stop it (cause {STALE_CAUSE}): {body.reason} "
        "Undo: none once it stops; run it again with `evo-agents worker triage rerun` once it has ended."
    )
    message_id = await said(conn, frame, text)
    await audited(conn, frame, audit.RUN_TRIAGE_CANCEL, "")
    return TriageCancelled(message_id=message_id, run_id=failed.id)


def with_edits(held_body: dict, edits: list[StepEdit]) -> dict:
    """The plan with the ``verify`` or ``what`` of each step of ``edits`` set as the call says; nothing else differs."""
    new = copy.deepcopy(held_body)
    for edit in edits:
        try:
            index = step_index(held_body, edit.step)
        except PlanProblem as exc:
            raise Unprocessable(str(exc)) from None
        for name in triage_rules.EDITABLE:
            value = getattr(edit, name)
            if value is not None:
                new["steps"][index][name] = value
    return new


async def write_edits(conn: AsyncConnection, frame: Frame, body: TriagePlanIn) -> tuple[int, int, list[str]]:
    """Write the edits as one revision under the owner's name: (the old revision, the new one, what each step edited
    said before, in the order of the edits)."""
    failed = frame.failed
    if failed.plan_id is None:
        raise Unprocessable(f"run {failed.id} has no plan to edit: it is a run that writes one; ask the owner")
    check_dispatcher(frame.access)
    await refuse_curator_plan(conn, frame.access, failed.plan_id)
    held = await plan_service.visible(conn, frame.access, failed.plan_id, None, lock=True)
    if held.revision != body.if_revision:
        raise plan_service.stale(frame.access, held, body.if_revision)
    edits = body.edits()
    new = with_edits(held.body, edits)
    problems = triage_rules.edit_problems(held.body, new, repos(failed))
    if problems:
        raise plan_models.PlanError(422, f"plan {held.plan_id}: {'; '.join(problems)}; nothing was written")
    plan_service.schema_checked(new, held.plan_id)
    owner = Principal(frame.owner.user_id, frame.owner.login, False, frame.owner.token_id, MACHINE, "")
    written = await plan_service.write_plan(conn, frame.access, owner, new, if_revision=held.revision)
    limit = min(OLD_VALUE_CHARS, OLD_VALUES_CHARS // len(edits))
    old = [old_values(held.body["steps"][step_index(held.body, edit.step)], edit, limit) for edit in edits]
    return held.revision, written.revision, old


async def edit_plan(conn: AsyncConnection, user: Principal, run_id: int, body: TriagePlanIn) -> tuple[int, TriageActed]:
    """Edit pending steps of the failed run's plan of the thread of chat run ``run_id`` for its owner, and with
    ``rerun`` run the failed step again: (the failed run's id, the answer). A refused rerun raises, so the caller's
    transaction leaves the plan as it was."""
    frame = await frame_of(conn, user, run_id)
    was, now, old = await write_edits(conn, frame, body)
    new_id = await rerun_failed(conn, frame, body, edited=True) if body.rerun else None
    again = f" Ran run #{frame.failed.id} again as run #{new_id}." if new_id else ""
    text = decision(frame.failed.plan_id, was, now, body.edits(), old, body.reason, again)
    message_id = await said(conn, frame, text)
    keys = ",".join(edit.step for edit in body.edits())
    tail = f" revision:{now}" + (f" rerun:{new_id}" if new_id else "")
    await audited(conn, frame, audit.RUN_TRIAGE_PLAN_EDIT, f" step:{keys}{tail}")
    return frame.failed.id, TriageActed(message_id=message_id, run_id=new_id, plan_revision=now)


async def invite(
    conn: AsyncConnection, user: Principal, run_id: int, body: TriageInviteIn
) -> tuple[int, TriageInvited | str]:
    """Invite the agent ``body.handle`` of another project, one the failed run's error names, into the chat of the
    thread of chat run ``run_id`` for the run's owner: (the failed run's id, the answer, or the refusal it said in the
    thread when the agent did not join)."""
    frame = await frame_of(conn, user, run_id)
    failed = frame.failed
    found = await candidates_of(
        conn,
        project=frame.project,
        project_id=frame.access.project_id,
        role=frame.access.role,  # pyright: ignore[reportArgumentType]
        owner_id=frame.owner.user_id,
        kind=failed.kind,
        state="failed",
        run_id=failed.id,
    )
    pointed = {o.project for o in found.other_projects if o.role is not None}  # a project with no grant: unseen
    profile = await by_handle(conn, body.handle)
    if profile is None or profile.project not in pointed or profile.deleted_at is not None:
        names = ", ".join(sorted(pointed)) or "none"
        raise Unprocessable(
            f"agent {body.handle} is no agent of a project that the error of run {failed.id} names and the owner "
            f"holds a grant on (projects: {names})",
        )
    other = next(o for o in found.other_projects if o.project == profile.project)
    chat = await chat_access(conn, frame.owner, frame.chat_id, lock=True)
    refusal = None
    if not other.fit:
        refusal = other.reasons[0]
    else:
        try:
            async with conn.begin_nested():
                added = await put_agent(conn, frame.owner, chat, profile)
        except HTTPException as exc:
            refusal = str(exc.detail)
    if refusal is not None:
        await said(
            conn, frame, f"No one invited: agent {body.handle} of project {profile.project} did not join: {refusal}"
        )
        await audited(conn, frame, audit.RUN_TRIAGE_INVITE, f" refused:@{body.handle}")
        reply = refusal
    else:
        text = (
            f"Decision: invited agent {body.handle} of project {profile.project} into this chat: {body.reason} "
            f"Undo: remove it from the chat. Work goes to it by a mention of the agent of this thread, one hop "
            f"further each time."
        )
        message_id = await said(conn, frame, text)
        await audited(conn, frame, audit.RUN_TRIAGE_INVITE, f" invited:@{body.handle}")
        reply = TriageInvited(message_id=message_id, project=profile.project, added=added)  # pyright: ignore[reportPossiblyUnboundVariable]
    return failed.id, reply
