"""Run a plan in an agent system: POST /v1/projects/{p}/plans/{plan_id}/system-dispatch (writer), body
``{"system": slug}``, hands the plan to the system's entry agent as the caller would by hand.

The hub writes the caller's own message in their direct chat with the entry agent (``chat_direct.direct_chat``, made
when there is none): it names the plan, its revision, its page on the web and its pending steps, and says how to run
it. Read the plan with plan_show; take the pending steps in the order their depends_on allows; hand each step to the
system's agents along its interacts edges with a brief (goal, repo, criteria, verify, done when); mark a step done
with its evidence once its verify passes; ask the person about any decision it may not take; and report which steps
are done and which are left. The message goes through ``chats.deliver_message`` as a typed one does, so it wakes the
entry agent and ``system_traces.join`` opens a trace at the system's revision now; the names the text mentions are
not read as mentions (``skip``), so the message hands work to the entry agent alone and opens no group. The trace
then takes the plan's id and revision (system_traces.plan_id and plan_revision, 0237), and the dispatch is audited
once (run.dispatch_system), naming the plan, the run, the system and the trace.

Refused with 403 without the writer role on the project (``dispatch.check_dispatcher``), 404 for a plan the caller
cannot read, 422 for a system the project lacks, and 409 for: a plan the Curator made (the night shift alone runs
it), a system with no entry agent, a plan with an active plan run or step run, a plan a trace runs whose runs have
not all ended, a plan with no pending step, and a message the entry agent took no run for, or whose run opened no
trace of the system (its refusal named). A refusal writes nothing: the message, the chat it would open and the run
roll back with it.

Every dispatch of a plan takes the plan's dispatch lock (``views.lock_plan``) before it reads the plan's activity, as
a plan run and a step run do, so two dispatches at once, or a dispatch and a plan run, give one trace or one run.
While a trace of the plan has a run queued, running, waiting or parked, a plan run and a step run of the plan get 409
naming the trace and its system, and ready-steps shows the trace (``views.plan_trace``); once every run of the trace
has ended, the plan is free again.

A chat run of the trace marks steps as any agent run does: with the MCP tool plan_step, which writes under the role of
the run's owner (``plans.patch``), the dispatcher here, a writer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, runs, system_context, tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server import audit, system_store
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chats import ChatMessageIn, deliver_message
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import REFUSALS, SystemDispatch, SystemDispatched
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat, no_plan_run, refuse_curator_plan
from evo_agents.hub.server.runs.service.views import busy, lock_plan, plan_activity
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})

MAX_TITLE_CHARS = 200  # of a step's title in the message
STEPS_ROOM = chats.MAX_MESSAGE_BYTES - 2048  # the bytes the list of pending steps may take, the rest of the text kept
NOT_SENT = "nothing was sent"


def pending_steps(body: dict) -> list[str]:
    """A line for each pending step of plan ``body``, in plan order: its key, title and the steps it depends on."""
    lines = []
    for index, step in enumerate(runs.plan_steps(body)):
        if isinstance(step, dict) and runs.step_status(step) != "pending":
            continue
        mapping = step if isinstance(step, dict) else {"title": step}
        title = " ".join(str(mapping.get("title") or "").split())[:MAX_TITLE_CHARS]
        after = mapping.get("depends_on")
        needs = f" (depends on {', '.join(str(dep) for dep in after)})" if isinstance(after, list) and after else ""
        lines.append(f"- step {step_key(step, index)}: {title}{needs}")
    return lines


def dispatch_text(plan_id: str, revision: int, link: str, slug: str, steps: list[str]) -> str:
    """The caller's message to the entry agent: the plan, its pending steps (as many as fit) and how to run it."""
    shown, used = [], 0
    for line in steps:
        used += len(line.encode()) + 1
        if used > STEPS_ROOM:
            shown.append(f"- and {len(steps) - len(shown)} more: see plan_show")
            break
        shown.append(line)
    return "\n".join(
        [
            f"Run plan {plan_id}, revision {revision}, in system {slug}: {link}",
            "",
            "Pending steps:",
            *shown,
            "",
            "How to run it:",
            f"1. Read the plan with plan_show (plan {plan_id}).",
            "2. Take the pending steps in the order their depends_on allows: a step is next once every step it "
            "depends on is done.",
            "3. Hand each step to the agents of the system along its interacts edges, with a brief: the goal, the "
            "repo, the criteria, the verify command and when it is done.",
            "4. When a step's verify passes, mark it done with plan_step, with the evidence.",
            "5. Ask me about any decision you may not take yourself.",
            "6. When you stop, report which steps are done and which are left.",
        ]
    )


async def _entry(conn: AsyncConnection, project_id: int, slug: str) -> tuple[system_store.System, str]:
    """The system ``slug`` of the project and the handle of its entry agent: 422 for no such system, 409 for one
    with no entry agent."""
    system = await system_store.one(conn, project_id, slug)
    if system is None:
        raise HTTPException(422, f"the project has no system {slug}: see GET /v1/projects/{{p}}/systems; {NOT_SENT}")
    ap = tables.agent_profiles
    handle = None
    if system.entry_profile_id is not None:
        query = select(ap.c.handle).where(ap.c.id == system.entry_profile_id)
        handle = (await conn.execute(query)).scalar_one_or_none()
    if handle is None:
        raise HTTPException(409, f"system {slug} has no entry agent to hand the plan to; {NOT_SENT}")
    return system, handle


async def _free(conn: AsyncConnection, access: ProjectAccess, plan_id: str) -> None:
    """Take the plan's dispatch lock, then 409 while it has an active plan run, step run or trace."""
    await lock_plan(conn, access.project_id, plan_id)
    activity = await plan_activity(conn, access.project_id, plan_id)
    no_plan_run(activity, plan_id)
    if activity.steps:
        key, active = min(activity.steps.items(), key=lambda item: item[1].id)
        raise HTTPException(409, f"step {key} of plan {plan_id} {busy(active)}: a system runs the plan once it ends")


async def _woken(conn: AsyncConnection, message_id: int, entry: str, system: system_store.System) -> tuple[int, int]:
    """(the run message ``message_id`` woke the entry agent ``entry`` of ``system`` with, the trace it opened); 409
    with the hub's refusal when it took no run, or its run opened no trace of the system."""
    m = tables.chat_mentions
    query = select(m.c.run_id, m.c.refusal).where(
        m.c.message_id == message_id, m.c.profile_id == system.entry_profile_id
    )
    mention = (await conn.execute(query)).first()
    if mention is None or mention.run_id is None:
        why = "" if mention is None or not mention.refusal else f": {mention.refusal}"
        raise HTTPException(409, f"@{entry} took no run for the plan{why}; {NOT_SENT}")
    s, t = tables.system_spans, tables.system_traces
    query = (
        select(t.c.id, t.c.system_id)
        .join_from(s, t, t.c.id == s.c.trace_id)
        .where(s.c.run_id == mention.run_id, s.c.name == system_context.INVOKE)
    )
    trace = (await conn.execute(query)).first()
    if trace is None or trace.system_id != system.id:
        raise HTTPException(409, f"run {mention.run_id} of @{entry} opened no trace of the system; {NOT_SENT}")
    return mention.run_id, trace.id


async def dispatch_to_system(
    conn: AsyncConnection, user: Principal, project: str, plan_id: str, slug: str, public_url: str | None
) -> SystemDispatched:
    """Hand plan ``plan_id`` to system ``slug`` in the caller's transaction, as the module's docstring says."""
    access = await project_access(conn, user, project)
    check_dispatcher(access)
    await check_seat(conn, user, "running a plan in a system")
    held = await plan_routes._visible(conn, access, plan_id, None)
    await refuse_curator_plan(conn, access, plan_id)
    system, entry = await _entry(conn, access.project_id, slug)
    await _free(conn, access, plan_id)
    steps = pending_steps(held.body)
    if not steps:
        raise HTTPException(409, f"plan {plan_id} has no pending step, so a system has nothing to run; {NOT_SENT}")
    link = f"{public_url or ''}/p/{project}/plans/{plan_id}"
    text = dispatch_text(plan_id, held.revision, link, slug, steps)
    chat_id, _ = await direct_chat(conn, user, entry)
    chat = await chat_access(conn, user, chat_id)
    skip = frozenset(chats.mention_names(text))  # the entry agent takes it as its direct chat's agent, and no other
    message_id = await deliver_message(conn, chat, user, ChatMessageIn(body=text, reply_to=None), skip=skip)
    run_id, trace_id = await _woken(conn, message_id, entry, system)
    t = tables.system_traces
    values = {"plan_id": plan_id, "plan_revision": held.revision}
    query = update(t).values(**values).where(t.c.id == trace_id).returning(t.c.system_revision)
    revision = (await conn.execute(query)).scalar_one()
    target = f"{run_target(project, plan_id, None, run_id)} system:{slug} trace:{trace_id}"
    await audit_run(conn, user, access, audit.RUN_DISPATCH_SYSTEM, target)
    return SystemDispatched(
        system=slug,
        system_revision=revision,
        trace_id=trace_id,
        plan_id=plan_id,
        plan_revision=held.revision,
        chat_id=chat_id,
        message_id=message_id,
        run_id=run_id,
    )


@router.post(
    "/{project}/plans/{plan_id}/system-dispatch",
    status_code=201,
    response_model=SystemDispatched,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def system_dispatch(
    request: Request, project: ProjectName, plan_id: plan_routes.PlanId, body: SystemDispatch, user: CurrentUser
) -> SystemDispatched:
    """Hand the plan to the system's entry agent: the caller's message in their direct chat with it names the plan and
    how to run it, and the trace it opens runs the plan."""
    async with request.app.state.engine.begin() as conn:
        found = await dispatch_to_system(conn, user, project, plan_id, body.system, request.app.state.config.public_url)
    log.info(
        "plan handed to a system",
        extra={"project": project, "plan_id": plan_id, "system": body.system, "trace_id": found.trace_id},
    )
    return found
