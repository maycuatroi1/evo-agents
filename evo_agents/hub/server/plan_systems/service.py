"""Hand a plan to the entry agent of a system of its project (``dispatch_to_system``), in the caller's transaction or
in one of its own (``dispatch``); the routes of ``evo_agents.hub.server.plan_systems`` say what it writes and refuses."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, runs
from evo_agents.hub.plans import step_key
from evo_agents.hub.server import audit, system_store
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chats import ChatMessageIn, deliver_message
from evo_agents.hub.server.plan_systems.repository import STORE
from evo_agents.hub.server.plans.service import visible
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import SystemDispatched
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat, no_plan_run, refuse_curator_plan
from evo_agents.hub.server.runs.service.views import busy, lock_plan, plan_activity
from evo_agents.hub.server.security import Principal

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
            "repo, the criteria, the verify command and when it is done. As you hand it, mark it in_progress with "
            "plan_step, with a note naming the agent and its run, so the plan's page shows it moving.",
            "4. When a step's verify passes, mark it done with plan_step, with the evidence; a step that comes back "
            "unfinished goes back to pending, with a note saying why.",
            "5. Ask me about any decision you may not take yourself.",
            "6. When you stop, report which steps are done and which are left.",
        ]
    )


async def _entry(conn: AsyncConnection, project_id: int, slug: str) -> tuple[system_store.System, str]:
    """The system ``slug`` of the project and the handle of its entry agent: 422 for no such system, 409 for one
    with no entry agent."""
    system = await system_store.one(conn, project_id, slug)
    if system is None:
        raise Unprocessable(f"the project has no system {slug}: see GET /v1/projects/{{p}}/systems; {NOT_SENT}")
    handle = None if system.entry_profile_id is None else await STORE.handle(conn, system.entry_profile_id)
    if handle is None:
        raise Conflict(f"system {slug} has no entry agent to hand the plan to; {NOT_SENT}")
    return system, handle


async def _free(conn: AsyncConnection, access: ProjectAccess, plan_id: str) -> None:
    """Take the plan's dispatch lock, then 409 while it has an active plan run, step run or trace."""
    await lock_plan(conn, access.project_id, plan_id)
    activity = await plan_activity(conn, access.project_id, plan_id)
    no_plan_run(activity, plan_id)
    if activity.steps:
        key, active = min(activity.steps.items(), key=lambda item: item[1].id)
        raise Conflict(f"step {key} of plan {plan_id} {busy(active)}: a system runs the plan once it ends")


async def _woken(conn: AsyncConnection, message_id: int, entry: str, system: system_store.System) -> tuple[int, int]:
    """(the run message ``message_id`` woke the entry agent ``entry`` of ``system`` with, the trace it opened); 409
    with the hub's refusal when it took no run, or its run opened no trace of the system."""
    mention = await STORE.mention(conn, message_id, system.entry_profile_id)
    if mention is None or mention.run_id is None:
        why = "" if mention is None or not mention.refusal else f": {mention.refusal}"
        raise Conflict(f"@{entry} took no run for the plan{why}; {NOT_SENT}")
    trace = await STORE.invoked_trace(conn, mention.run_id)
    if trace is None or trace.system_id != system.id:
        raise Conflict(f"run {mention.run_id} of @{entry} opened no trace of the system; {NOT_SENT}")
    return mention.run_id, trace.id


async def dispatch_to_system(
    conn: AsyncConnection, user: Principal, project: str, plan_id: str, slug: str, public_url: str | None
) -> SystemDispatched:
    """Hand plan ``plan_id`` to system ``slug`` in the caller's transaction, as the routes' docstring says."""
    access = await project_access(conn, user, project)
    check_dispatcher(access)
    await check_seat(conn, user, "running a plan in a system")
    held = await visible(conn, access, plan_id, None)
    await refuse_curator_plan(conn, access, plan_id)
    system, entry = await _entry(conn, access.project_id, slug)
    await _free(conn, access, plan_id)
    steps = pending_steps(held.body)
    if not steps:
        raise Conflict(f"plan {plan_id} has no pending step, so a system has nothing to run; {NOT_SENT}")
    link = f"{public_url or ''}/p/{project}/plans/{plan_id}"
    text = dispatch_text(plan_id, held.revision, link, slug, steps)
    chat_id, _ = await direct_chat(conn, user, entry)
    chat = await chat_access(conn, user, chat_id)
    skip = frozenset(chats.mention_names(text))  # the entry agent takes it as its direct chat's agent, and no other
    message_id = await deliver_message(conn, chat, user, ChatMessageIn(body=text, reply_to=None), skip=skip)
    run_id, trace_id = await _woken(conn, message_id, entry, system)
    revision = await STORE.take_plan(conn, trace_id, plan_id, held.revision)
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


async def dispatch(
    engine: AsyncEngine, user: Principal, project: str, plan_id: str, slug: str, public_url: str | None
) -> SystemDispatched:
    """``dispatch_to_system`` in a transaction of its own, which a refusal rolls back."""
    async with UnitOfWork(engine) as conn:
        return await dispatch_to_system(conn, user, project, plan_id, slug, public_url)
