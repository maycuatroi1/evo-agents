"""An agent asks an agent of another project (``evo-agents worker agent ask``, ``chat_runs.ASK_COMMAND``): POST
/v1/worker/runs/{run}/agent/asks, and the answer the hub brings back. ``docs/hub.md`` (Agents across projects) is the
design.

The route takes the worker token of the worker that holds a chat run (404 for a run it does not hold, 422 for a run of
another kind). It refuses, with why, before it writes anything: an ask of the run's own agent
(``chat_runs.hop_problem``; hops have no limit), an agent the person at the origin of the chain cannot see, one that takes no work
(``agent_tasking.work_home``: deleted, or no pool of its own), one that person may not give work
(``agent_tasking.task_refusal``: the writer rule of a mention). A refusal is answered (201, ``refusal``) and said in the
run's thread.

Else, in a savepoint (``work_chats.hand_over``), the hub puts the question as a message of the asking run's agent,
naming the asking run: in the work chat of the asking run's trace of an agent system, in the thread of the agent asked
(``docs/hub.md``, Work chats), the thread of the plan step its brief names (``--ref plan:<id>/step:<key>``) when it
names one, so each step runs in a session of its own and independent steps take free slots of the agent's pool at once,
else in the origin's direct chat with the agent asked (``chats.service.direct.direct_chat``),
where it starts a thread: the run
it hands (``chats.service.runs.hand_work``) is of the agent asked, on its pool, dispatched by the origin, at one hop more, and
acts within the asking run's principal and autonomy snapshot (``run_autonomy.of_message``), so each run of a chain has
no more rights than the run before it. A refusal of ``hand_work`` rolls the savepoint back. The new run names the
asking run in ``spawned_by_run_id`` and the wait it opens on the asking run in ``spawned_wait_id`` (a decision of
category wait that wakes at ``run_waits.MAX_WAIT`` at the latest and notifies nobody): the worker's command notes it,
the agent ends its turn, and the run parks as for any wait, holding no worker; a run asked again while it goes on
keeps the wait it has, and each wait names the run asked in ``decisions.awaits_run_id``. The hub says which agent it
asked, in which project, and what, in the asking run's thread, or in the work chat when the trace has one
(``work_chats.say``).

When the run asked ends (``answer_asker``, from ``chats.service.runs.chat_run_moved``), the hub answers that wait
(``run_waits.wake``), which resumes the asking run in its session, with ``delegated_answer``: the last message the
run asked, or a run it went on from, wrote in its thread, past the hub's lines of its pushes (``last_reply``), whole,
and the refs of its work (its outputs, its branch and repos; the knowledge graph nodes are named in its answer, as the
question asks), or why it failed or was cancelled. The same text goes where the asked line went, once an asker, cut
to ANSWER_IN_THREAD with the id of the message that holds it whole. A run asked that goes on in its session (a parked run resumed, an inbox untaken)
carries both columns, and every wait whose ``awaits_run_id`` is in its chain of resumed runs is answered too, so the
run that really ends answers each ask that reached it. A run lost is no end: its next attempt carries both columns and
its chain goes back through ``parent_run_id``, and the run lost answers nothing (``chats.service.runs.chat_run_moved``), since its
worker may come back for it (``run_reattach``), which cancels that attempt and has the run go on with its waits open. A wait answered already, by its time or by a person, is left as it is.
Waits that close a cycle, two runs each waiting on the other's end, are broken by the reaper (``wait_cycles``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, kg_labels, runs, system_context
from evo_agents.hub.server import audit, cross_labels, run_waits
from evo_agents.hub.server.agent_accounts.service.mask import mask_run, mask_values
from evo_agents.hub.server.agents.models.asks import (
    ANSWER_IN_THREAD,
    CHAIN_MOST,
    LAST_REPLY_SCAN,
    THREAD_CUT_MARK,
    AgentAsked,
    AgentAskIn,
    Answer,
)
from evo_agents.hub.server.agents.repository import asks as repository
from evo_agents.hub.server.agents.repository.tasking import project_role
from evo_agents.hub.server.agents.service import tasking as agent_tasking
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.service.agents import AgentRun, agent_run
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.runs import agent_access, chat_run, hub_reply
from evo_agents.hub.server.decisions.models import OptionIn
from evo_agents.hub.server.decisions.service.asks import store_decision
from evo_agents.hub.server.outputs.service import store as output_store
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.projects import access_of
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.work_chats import service as work_chats

log = logging.getLogger(__name__)


# Asking


async def _refusal(conn: AsyncConnection, run: AgentRun, target: Profile | None, handle: str) -> str | None:
    """Why the agent of ``run`` may not ask agent ``target`` (named ``handle``) for the origin of its chain; None when
    it may."""
    origin = run.principal
    if target is None or await project_role(conn, origin.user_id, target.project_id) is None:
        return f"no agent @{handle} that {origin.login} can see"
    problem = rules.hop_problem(run.hop + 1, target.id == run.profile.id)
    if problem is not None:
        return problem
    refusal = await agent_tasking.task_refusal(conn, target, origin.user_id, origin.login)
    if refusal is not None:
        return refusal
    _, why = await agent_tasking.work_home(conn, target)
    return why


def question_text(run: AgentRun, project: str, target: Profile, text: str, brief: dict | None = None) -> str:
    """The message that asks agent ``target`` in the origin's direct chat with it: what the asking agent wrote, its
    brief when it gave one (``system_context.brief_text``), and what its answer should hold."""
    shown = system_context.brief_text(brief)
    asked = (
        f"@{target.handle}, @{run.profile.handle} of project {project} asks this for {run.principal.login} (run "
        f"#{run.run_id}):\n\n{text.strip()}\n\n"
        + (f"{shown}\n\n" if shown else "")
        + "Your last message of this turn is the answer it gets: what you found or did, the files, outputs and branch "
        "of your work, and the knowledge graph nodes it rests on (repo:path or repo:path::Symbol)."
    )
    return runs.clip(asked, chats.MAX_MESSAGE_BYTES)


async def _system_refusal(
    conn: AsyncConnection, found: system_traces.Membership | None, target: Profile, reason: str | None
) -> tuple[str | None, bool]:
    """(why the ask of ``target`` by a run of an agent system is refused, or None; whether it is off design): past the
    budget of its trace, or off its agent's interacts edges with no ``reason`` (``system_context.off_edge_refusal``).
    An agent of no system asks as before."""
    if found is None or found.node is None:
        return None, False
    over = await system_traces.budget_problem(conn, found)
    if over is not None:
        return over, False
    targets = system_context.edge_targets(found.spec, found.handle)
    if target.handle in targets:
        return None, False
    if not (reason or "").strip():
        return system_context.off_edge_refusal(found.handle, target.handle, targets), True
    return None, True


async def _hand(
    conn: AsyncConnection, run: AgentRun, target: Profile, text: str, priority: str, brief: dict
) -> tuple[int | None, int | None, str | None, tuple[int, int] | None]:
    """(the run of ``target`` handed, the question's message, None, the thread it is in), or (None, None, why none,
    None), in a savepoint rolled back on a refusal (``work_chats.hand_over``); an ask whose ``brief`` names a plan step
    goes to the session of that step."""
    asker = work_chats.Asker(run.profile, run.run_id, run.principal, run.via, run.hop + 1, priority)
    handed = await work_chats.hand_over(conn, asker, target, text, system_traces.ref_steps(brief))
    return handed.child, handed.message_id, handed.why, handed.place


async def open_wait(conn: AsyncConnection, row, run: AgentRun, target: Profile, child: int) -> int:
    """The wait of the asking run (``row``, as ``held_plan_run`` reads it) that the end of run ``child`` answers; its
    id."""
    wake_at = datetime.now(UTC) + run_waits.MAX_WAIT
    what = f"@{target.handle} of project {target.project} (run #{child})"
    ask = run_waits.WaitAsk(
        category=runs.WAIT_DECISION_CATEGORY,
        question=f"The answer of {what}, which the agent asked.",
        context="\n".join(
            [
                "## What the run waits for",
                "",
                f"- The end of run #{child} of {what}: its answer, or why it failed.",
                f"- At the latest, its time: {wake_at:%Y-%m-%d %H:%M} UTC.",
                "",
                "The hub answers this wait when that run ends, and the agent goes on in its session. Nobody needs to "
                "answer it; Go on now wakes the run sooner, and Stop waiting has it go on without the answer.",
            ]
        ),
        options=[OptionIn(key=key, label=label, description=text) for key, label, text in run_waits.OPTIONS],
    )
    view = await store_decision(conn, run.run_id, row, ask, wake={"wake_at": wake_at, "wake_on": None})
    await repository.mark_asked(conn, child, run.run_id, view.id)
    return view.id


async def ask_agent(
    conn: AsyncConnection, sealer, run: AgentRun, body: AgentAskIn, row, *, tool: str = system_context.TOOL_ASK
) -> AgentAsked:
    """Ask agent ``body.handle`` as the agent of chat run ``run`` (its locked ``row``, as ``held_plan_run`` reads it),
    with the friction and the trace budget of its agent system, recorded as span tool ``tool``; the route above and
    the hub runtime's ask and escalate tools (``hub_tools``) share it."""
    run_id = run.run_id
    chat = await chat_run(conn, run_id)
    assert chat is not None  # every chat run starts from a chat message (hand_work)
    said = await mask_values(conn, sealer, run.profile.id, body.body)
    said = await mask_run(conn, sealer, run_id, said)
    target = await by_handle(conn, body.handle)
    refusal = await _refusal(conn, run, target, body.handle)
    child = decision_id = message_id = place = None
    found = await system_traces.of_run(conn, run_id)
    off = False
    if refusal is None:
        assert target is not None  # _refusal refuses a handle no agent has
        refusal, off = await _system_refusal(conn, found, target, body.reason)
    brief = body.brief()
    if refusal is None:
        assert target is not None
        text = question_text(run, run.profile.project, target, said, brief)
        child, message_id, refusal, place = await _hand(conn, run, target, text, body.priority, brief)
    if refusal is None:
        assert target is not None and child is not None  # _hand made the run or gave its refusal
        decision_id = await open_wait(conn, row, run, target, child)
        await system_traces.save_brief(conn, child, run_id, brief)
        if off:
            await system_traces.mark_reason(conn, child, (body.reason or "").strip())
        shown = f"@{run.profile.handle} asked @{target.handle} of project {target.project} (run #{child}): {said}"
        target_of = f"message:{message_id} run:{run_id} asked:{child}"
        await Auditor(conn, None, run.principal).record(audit.CHAT_MESSAGE, target_of)
    else:
        shown = f"@{body.handle}: {refusal}"
    if found is not None:
        await system_traces.record_tool(
            conn,
            found,
            tool,
            to_handle=target.handle if target is not None else body.handle,
            status="ok" if refusal is None else "refused",
            off_design=off,
            reason=(body.reason or "").strip() or (refusal if refusal is not None else None),
            message_id=message_id,
            attributes={
                "evo.brief_fields": system_context.brief_present(brief),
                **({"evo.child_run_id": child} if child is not None else {}),
            },
        )
    if child is not None:
        from evo_agents.hub.server.orchestrator.service import (
            threads as orchestrator,  # it hands work through chat_runs, as this module does
        )

        await orchestrator.check_loop(conn, child)  # a question a session took in its inbox joined no trace again
    await work_chats.say(conn, run_id, (chat.chat_id, chat.root_id), shown, place, repeats=message_id)
    log.info("agent asked", extra={"run_id": run_id, "handle": body.handle, "made": child, "refused": bool(refusal)})
    return AgentAsked(run_id=child, decision_id=decision_id, refusal=refusal)


# The answer


async def last_reply(conn: AsyncConnection, run_id: int) -> tuple[int, str] | None:
    """(id, body) of the last message the agent of run ``run_id`` wrote in the thread of the message that started it,
    as that run or a run it went on from (``_chain``): a later run of the same agent in the thread is another answer.
    It steps over the hub's lines of its pushes (``is_pull_text``), which come after its answer when the worker pushes
    at the end of the turn, one a repo."""
    row = await repository.started_in(conn, run_id)
    if row is None:
        return None
    chain = await _chain(conn, run_id)
    for found in await repository.written_in(conn, row.root, row.profile_id, chain, LAST_REPLY_SCAN):
        if not rules.is_pull_text(found.body):
            return found.id, found.body
    return None


async def _chain(conn: AsyncConnection, run_id: int) -> list[int]:
    """Run ``run_id`` and the runs it went on from, in its session (``resume_of_run_id``) or after a loss
    (``parent_run_id``), newest first, at most CHAIN_MOST."""
    chain: list[int] = []
    at: int | None = run_id
    while at is not None and at not in chain and len(chain) < CHAIN_MOST:
        chain.append(at)
        row = await repository.went_on_from(conn, at)
        at = None if row is None else row.resume_of_run_id or row.parent_run_id
    return chain


async def _refs(conn: AsyncConnection, run_id: int, wait_id: int) -> list[str]:
    """The refs of the work of the runs that answer wait ``wait_id``: their outputs, and the branch and repos of run
    ``run_id``, the last of them."""
    made = await repository.wait_outputs(conn, wait_id)
    row = await repository.run_work(conn, run_id)
    refs = []
    if made:
        refs.append("Outputs: " + ", ".join(f"{item.name} (#{item.id})" for item in made))
    repos = [entry["repo"] for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo")]
    if row.branch and repos:
        commit = f" at {row.commit_sha[:12]}" if row.commit_sha else ""
        refs.append(f"Branch: {row.branch} of {', '.join(repos)}{commit}")
    return refs


async def withheld_answer(conn: AsyncConnection, run_id: int, wait_id: int) -> str | None:
    """Why what run ``run_id`` says may not enter the run whose wait ``wait_id`` it answers; None when it may. The check
    of every read across projects (``cross_labels``): the hub sink of the asking run's project must let in the label of
    the run asked (its plan's, else its project's default label, ``kg_labels.run_label``). Within one project, nothing
    crosses."""
    return await withheld_from(conn, run_id, await repository.wait_project(conn, wait_id))


async def withheld_from(conn: AsyncConnection, run_id: int, project_id: int) -> str | None:
    """Why what run ``run_id`` says may not enter project ``project_id`` (``withheld_answer``); None when it may."""
    source, target = await repository.project_names(conn, run_id, project_id)
    if source == target:
        return None
    target_access, source_access = await access_of(conn, None, target), await access_of(conn, None, source)
    assert target_access is not None and source_access is not None  # both hold a run
    gate = cross_labels.gate(target_access.rules, source_access.rules)
    return gate.why(await kg_labels.run_label(conn, run_id))


async def delegated_answer(conn: AsyncConnection, run_id: int, state: str, wait_id: int, handle: str) -> Answer:
    """What the asking run gets, and its thread shows, when run ``run_id`` of agent ``handle`` it asked ended in
    ``state``: its answer and the refs of its work, or why it gave none. The one place where text crosses from the
    project asked to the asking run's, so the one that checks it (``withheld_answer``): an answer the asking run's
    project does not let in is left out whole, its refs and its failure too, and both say why."""
    row = await repository.run_end(conn, run_id)
    why = await withheld_answer(conn, run_id, wait_id)
    if why is not None:
        said = f"run #{run_id} of @{handle}, which you asked, ended {state}, and the hub withheld what it said: {why}"
        agent = f"Decision #{wait_id} (wait) woke: {said}. Say so in your reply; asking again gets the same."
        thread = f"@{handle} ended run #{run_id} {state}; its answer was withheld from this run: {why}"
        return Answer(runs.clip(agent, runs.MAX_ANSWER_BYTES), runs.clip(thread, ANSWER_IN_THREAD))
    if state != "done":
        given = "it was cancelled" if state == "cancelled" else "its worker gave no reason"
        why = row.error or row.failure_cause or given
        said = f"run #{run_id} of @{handle}, which you asked, ended {state}: {why}"
        agent = f"Decision #{wait_id} (wait) woke: {said}. It gave no answer: say so in your reply, or ask again."
        thread = f"@{handle} gave no answer: run #{run_id} ended {state}: {why}"
        return Answer(runs.clip(agent, runs.MAX_ANSWER_BYTES), runs.clip(thread, ANSWER_IN_THREAD))
    reply = await last_reply(conn, run_id)
    lines = [reply[1].strip() if reply else "(it wrote no message in its thread)"]
    refs = await _refs(conn, run_id, wait_id)
    lines += ["", *refs] if refs else []
    body = "\n".join(lines)
    agent = f"Decision #{wait_id} (wait) woke: run #{run_id} of @{handle}, which you asked, answered:\n\n{body}"
    thread = f"@{handle} answered (run #{run_id}):\n\n{body}"
    # the agent gets the answer whole, a chat message of at most 16 KiB; the thread shows its start
    mark = runs.TRUNCATION_MARK if reply is None else THREAD_CUT_MARK.replace("{message}", str(reply[0]))
    repeats = None if reply is None else reply[0]
    return Answer(runs.clip(agent, runs.MAX_INBOX_BYTES), runs.clip(thread, ANSWER_IN_THREAD, mark), repeats)


async def answer_asker(conn: AsyncConnection, run_id: int, state: str, handle: str) -> int | None:
    """When run ``run_id`` of agent ``handle``, asked by another agent's run, ended in ``state``: each wait it answers
    (``repository.waits``) answered with ``delegated_answer``, in the caller's transaction, and the answer said where the hub's
    lines of the ask go (``work_chats.line_place``); the run that takes the first answer, None when nothing waits for
    it."""
    asked = await chat_run(conn, run_id)
    there = None if asked is None else (asked.chat_id, asked.root_id)
    woke: list[int] = []
    for wait in await repository.open_waits(conn, run_id):
        answer = await delegated_answer(conn, run_id, state, wait.id, handle)
        asker = await run_waits.wake(conn, wait.id, wait.project_id, wait.plan_id, answer.agent)
        if asker is None:
            continue
        found = await chat_run(conn, asker)
        if found is not None and asker not in woke:  # one line an asker, however often it asked
            await work_chats.say(
                conn, asker, (found.chat_id, found.root_id), answer.thread, there, repeats=answer.repeats
            )
        log.info("asked agent answered", extra={"run_id": run_id, "decision_id": wait.id, "asker": asker})
        woke.append(asker)
    return woke[0] if woke else None


# Forwarding


async def _forward_refusal(conn: AsyncConnection, found: system_traces.Membership | None, child_id: int) -> str | None:
    """Why the run of ``found`` may not forward the answer of run ``child_id``; None when it may: both runs are in
    one trace of an agent system, the child is another run, and it has ended."""
    if found is None or found.trace_id is None:
        return "this run is in no trace of an agent system: only a run of a system forwards an answer"
    if child_id == found.run_id:
        return "a run forwards the answer of another run of its trace, not its own"
    child = await system_traces.of_run(conn, child_id)
    if child is None or child.trace_id != found.trace_id:
        return f"run #{child_id} is not in the trace of this run (#{found.trace_id}): only its runs can be forwarded"
    state = await repository.run_column(conn, child_id, "state")
    if state not in runs.TERMINAL_STATES:
        return f"run #{child_id} has not ended ({state}): forward its answer once it has"
    return None


async def forward(conn: AsyncConnection, run: AgentRun, child_id: int) -> tuple[int | None, str | None]:
    """Post the last answer of run ``child_id`` verbatim, with the files it carries (an interactive answer
    included), in the thread of chat run ``run``: its author is the child's agent and its run ``run``'s, so the chat
    shows who forwarded it (``GroupMessage.forwarded_by``). (the message, None), or (None, why not)."""
    chat = await chat_run(conn, run.run_id)
    assert chat is not None  # the run of an agent that forwards is a chat run (hand_work)
    found = await system_traces.of_run(conn, run.run_id)
    refusal = await _forward_refusal(conn, found, child_id)
    reply = None
    if refusal is None:
        reply = await last_reply(conn, child_id)
        if reply is None:
            refusal = f"run #{child_id} wrote no answer to forward"
    if refusal is None:
        project_id = await repository.run_column(conn, run.run_id, "project_id")
        refusal = await withheld_from(conn, child_id, project_id)
    access = None
    if refusal is None:
        access = await agent_access(conn, chat.chat_id, run.profile, chat.dispatcher_id)
        if access is None:
            refusal = f"@{run.profile.handle} is no longer in this chat"
    message_id = None
    child_profile = None
    if refusal is None:
        assert reply is not None and access is not None
        child_profile = await repository.run_column(conn, child_id, "profile_id")
        message_id, _ = await insert_message(
            conn, access, reply[1], thread_root_id=chat.root_id, author_profile_id=child_profile, run_id=run.run_id
        )
        await output_store.link_outputs(conn, message_id, await repository.carried_outputs(conn, reply[0]))
    else:
        await hub_reply(conn, chat.chat_id, chat.root_id, f"Forward of run #{child_id}: {refusal}")
    if found is not None:
        await system_traces.record_tool(
            conn,
            found,
            system_context.TOOL_FORWARD,
            status="ok" if refusal is None else "refused",
            reason=refusal,
            message_id=message_id,
            attributes={"evo.forwarded_run_id": child_id},
        )
    return message_id, refusal


async def ask(engine: AsyncEngine, sealer, user, run_id: int, body: AgentAskIn) -> AgentAsked:
    """Ask agent ``body.handle`` of another project, in chat run ``run_id`` the worker of ``user`` holds; 422 for a
    run of another kind."""
    async with UnitOfWork(engine) as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise Unprocessable(f"run {run_id} is a {run.kind} run: only a chat run's agent asks another agent")
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
        return await ask_agent(conn, sealer, run, body, row)


async def forward_of(engine: AsyncEngine, user, run_id: int, child_id: int) -> tuple[int | None, str | None]:
    """``forward`` of the answer of run ``child_id`` into chat run ``run_id`` the worker of ``user`` holds; 422 for a
    run of another kind."""
    async with UnitOfWork(engine) as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise Unprocessable(f"run {run_id} is a {run.kind} run: only a chat run's agent forwards an answer")
        return await forward(conn, run, child_id)
