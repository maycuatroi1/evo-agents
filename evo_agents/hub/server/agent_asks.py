"""An agent asks an agent of another project (``evo-agents worker agent ask``, ``chat_runs.ASK_COMMAND``): POST
/v1/worker/runs/{run}/agent/asks, and the answer the hub brings back. ``docs/hub.md`` (Agents across projects) is the
design.

The route takes the worker token of the worker that holds a chat run (404 for a run it does not hold, 422 for a run of
another kind). It refuses, with why, before it writes anything: past ``chat_runs.MAX_HOPS`` or to the run's own agent
(``chat_runs.hop_problem``), an agent the person at the origin of the chain cannot see, one that takes no work
(``agent_tasking.work_home``: deleted, or no pool of its own), one that person may not give work
(``agent_tasking.task_refusal``: the writer rule of a mention). A refusal is answered (201, ``refusal``) and said in the
run's thread.

Else, in a savepoint (``work_chats.hand_over``), the hub puts the question as a message of the asking run's agent,
naming the asking run: in the work chat of the asking run's trace of an agent system, in the thread of the agent asked
(``docs/hub.md``, Work chats), else in the origin's direct chat with the agent asked (``chat_direct.direct_chat``),
where it starts a thread: the run
it hands (``chat_runs.hand_work``) is of the agent asked, on its pool, dispatched by the origin, at one hop more, and
acts within the asking run's principal and autonomy snapshot (``run_autonomy.of_message``), so each run of a chain has
no more rights than the run before it. A refusal of ``hand_work`` rolls the savepoint back. The new run names the
asking run in ``spawned_by_run_id`` and the wait it opens on the asking run in ``spawned_wait_id`` (a decision of
category wait that wakes at ``run_waits.MAX_WAIT`` at the latest and notifies nobody): the worker's command notes it,
the agent ends its turn, and the run parks as for any wait, holding no worker; a run asked again while it goes on
keeps the wait it has, and each wait names the run asked in ``decisions.awaits_run_id``. The hub says which agent it
asked, in which project, and what, in the asking run's thread, or in the work chat when the trace has one
(``work_chats.say``).

When the run asked ends (``answer_asker``, from ``chat_runs.chat_run_moved``), the hub answers that wait
(``run_waits.wake``), which resumes the asking run in its session, with ``delegated_answer``: the last message of the
agent asked in its thread and the refs of its work (its outputs, its branch and repos; the knowledge graph nodes are
named in its answer, as the question asks), or why it failed, was cancelled or lost. The same text goes where the
asked line went, once an asker. A run asked that goes on in its session (a parked run resumed, an inbox untaken)
carries both columns, and every wait whose ``awaits_run_id`` is in its chain of resumed runs is answered too, so the
run that really ends answers each ask that reached it. A wait answered already, by its time or by a person, is left as it is.
Waits that close a cycle, two runs each waiting on the other's end, are broken by the reaper (``wait_cycles``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, kg_labels, runs, system_context, tables
from evo_agents.hub.server import (
    agent_tasking,
    audit,
    cross_labels,
    output_store,
    run_waits,
    system_traces,
    work_chats,
)
from evo_agents.hub.server.agent_account_mask import mask_run, mask_values
from evo_agents.hub.server.chat_agents import REFUSALS, AgentRun, Handle, agent_run
from evo_agents.hub.server.chat_runs import agent_access, chat_run, hub_reply
from evo_agents.hub.server.chat_store import insert_message
from evo_agents.hub.server.decisions import OptionIn, store_decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.projects import access_of
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

ANSWER_IN_THREAD = 2048  # bytes of the answer the asking run's thread shows; the agent gets it whole
LAST_REPLY_SCAN = 8  # the last messages of an agent last_reply reads to step over the lines of its pushes

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class AgentAskIn(BaseModel):
    handle: Handle = Field(description="the agent asked, of another project, @ left out")
    body: str = Field(min_length=1, description=f"what it is asked, at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    goal: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what the agent asked should reach"
    )
    output: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what its answer should hold"
    )
    bounds: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what it should not do or touch"
    )
    done_when: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: when its work is finished"
    )
    refs: list[str] = Field(
        default_factory=list,
        max_length=system_context.MAX_REFS,
        description="the brief: refs it starts from (repo:path, a run, an output, a URL)",
    )
    reason: str | None = Field(
        None,
        max_length=system_context.REASON_CHARS,
        description="why an agent of a system asks an agent off its interacts edges; without it such an ask is refused",
    )

    def brief(self) -> dict:
        return system_context.clean_brief(self.model_dump(include={*system_context.BRIEF_FIELDS, "refs"}))

    @field_validator("body")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a question has some text")
        if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a question is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return value


class AgentAsked(BaseModel):
    run_id: int | None = Field(description="the run of the agent asked; null when the hub refused")
    decision_id: int | None = Field(description="the wait of the asking run its answer wakes; null when refused")
    refusal: str | None = Field(description="why the hub handed no work, as the thread says too")


# Asking


async def _refusal(conn: AsyncConnection, run: AgentRun, target: Profile | None, handle: str) -> str | None:
    """Why the agent of ``run`` may not ask agent ``target`` (named ``handle``) for the origin of its chain; None when
    it may."""
    origin = run.principal
    if target is None or await agent_tasking.project_role(conn, origin.user_id, target.project_id) is None:
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
    conn: AsyncConnection, run: AgentRun, target: Profile, text: str
) -> tuple[int | None, int | None, str | None, tuple[int, int] | None]:
    """(the run of ``target`` handed, the question's message, None, the thread it is in), or (None, None, why none,
    None), in a savepoint rolled back on a refusal (``work_chats.hand_over``)."""
    asker = work_chats.Asker(run.profile, run.run_id, run.principal, run.via, run.hop + 1)
    handed = await work_chats.hand_over(conn, asker, target, text)
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
    r, d = tables.runs, tables.decisions
    held = exists().where(d.c.id == r.c.spawned_wait_id, d.c.state == "open")  # a wait it answers already stays
    await conn.execute(
        update(r).values(spawned_by_run_id=run.run_id, spawned_wait_id=view.id).where(r.c.id == child, ~held)
    )
    await conn.execute(update(d).values(awaits_run_id=child).where(d.c.id == view.id))
    return view.id


@worker_router.post(
    "/runs/{run_id}/agent/asks",
    status_code=201,
    response_model=AgentAsked,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: AgentAskIn, user: CurrentUser) -> AgentAsked:
    """Ask agent ``handle`` of another project, in a chat run this worker holds (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise HTTPException(422, f"run {run_id} is a {run.kind} run: only a chat run's agent asks another agent")
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("chat",))
        asked = await ask_agent(conn, request.app.state.sealer, run, body, row)
    return asked


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
        child, message_id, refusal, place = await _hand(conn, run, target, text)
    if refusal is None:
        assert target is not None and child is not None  # _hand made the run or gave its refusal
        decision_id = await open_wait(conn, row, run, target, child)
        await system_traces.save_brief(conn, child, run_id, brief)
        if off:
            await system_traces.mark_reason(conn, child, (body.reason or "").strip())
        shown = f"@{run.profile.handle} asked @{target.handle} of project {target.project} (run #{child}): {said}"
        await audit.record(
            conn,
            actor_id=run.principal.user_id,
            token_id=run.principal.token_id,
            action=audit.CHAT_MESSAGE,
            target=f"message:{message_id} run:{run_id} asked:{child}",
        )
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
        from evo_agents.hub.server import orchestrator  # it hands work through chat_runs, as this module does

        await orchestrator.check_loop(conn, child)  # a question a session took in its inbox joined no trace again
    await work_chats.say(conn, run_id, (chat.chat_id, chat.root_id), shown, place)
    log.info("agent asked", extra={"run_id": run_id, "handle": body.handle, "made": child, "refused": bool(refusal)})
    return AgentAsked(run_id=child, decision_id=decision_id, refusal=refusal)


# The answer


async def _last_reply(conn: AsyncConnection, run_id: int) -> str | None:
    """The last message of the agent of run ``run_id`` in the thread of the message that started it."""
    found = await last_reply(conn, run_id)
    return None if found is None else found[1]


async def last_reply(conn: AsyncConnection, run_id: int) -> tuple[int, str] | None:
    """(id, body) of the last message of the agent of run ``run_id`` in the thread of the message that started it,
    past the hub's lines of its pushes (``is_pull_text``), which come after its answer when the worker pushes at the end
    of the turn."""
    r, m = tables.runs, tables.chat_messages
    row = (
        await conn.execute(
            select(r.c.profile_id, func.coalesce(m.c.thread_root_id, m.c.id).label("root"))
            .join_from(r, m, m.c.id == r.c.chat_message_id)
            .where(r.c.id == run_id)
        )
    ).one_or_none()
    if row is None:
        return None
    in_thread = func.coalesce(m.c.thread_root_id, m.c.id) == row.root
    query = select(m.c.id, m.c.body).where(in_thread, m.c.author_profile_id == row.profile_id).order_by(m.c.id.desc())
    for found in (await conn.execute(query.limit(LAST_REPLY_SCAN))).all():
        if not rules.is_pull_text(found.body):
            return found.id, found.body
    return None


async def _refs(conn: AsyncConnection, run_id: int, wait_id: int) -> list[str]:
    """The refs of the work of the runs that answer wait ``wait_id``: their outputs, and the branch and repos of run
    ``run_id``, the last of them."""
    r, o = tables.runs, tables.run_outputs
    chain = select(r.c.id).where(r.c.spawned_wait_id == wait_id)
    query = select(o.c.id, o.c.name).where(o.c.run_id.in_(chain)).order_by(o.c.id)
    made = (await conn.execute(query)).all()
    row = (await conn.execute(select(r.c.branch, r.c.repos, r.c.commit_sha).where(r.c.id == run_id))).one()
    refs = []
    if made:
        refs.append("Outputs: " + ", ".join(f"{item.name} (#{item.id})" for item in made))
    repos = [entry["repo"] for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo")]
    if row.branch and repos:
        commit = f" at {row.commit_sha[:12]}" if row.commit_sha else ""
        refs.append(f"Branch: {row.branch} of {', '.join(repos)}{commit}")
    return refs


@dataclass(frozen=True)
class Answer:
    """What the asking run gets (``agent``), the message that wakes its agent, and what its thread shows
    (``thread``)."""

    agent: str
    thread: str


async def withheld_answer(conn: AsyncConnection, run_id: int, wait_id: int) -> str | None:
    """Why what run ``run_id`` says may not enter the run whose wait ``wait_id`` it answers; None when it may. The check
    of every read across projects (``cross_labels``): the hub sink of the asking run's project must let in the label of
    the run asked (its plan's, else its project's default label, ``kg_labels.run_label``). Within one project, nothing
    crosses."""
    d = tables.decisions
    project_id = (await conn.execute(select(d.c.project_id).where(d.c.id == wait_id))).scalar_one()
    return await withheld_from(conn, run_id, project_id)


async def withheld_from(conn: AsyncConnection, run_id: int, project_id: int) -> str | None:
    """Why what run ``run_id`` says may not enter project ``project_id`` (``withheld_answer``); None when it may."""
    r, p = tables.runs, tables.projects
    asked = select(p.c.name).join_from(r, p, p.c.id == r.c.project_id).where(r.c.id == run_id)
    asking = select(p.c.name).where(p.c.id == project_id)
    source, target = (await conn.execute(asked)).scalar_one(), (await conn.execute(asking)).scalar_one()
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
    r = tables.runs
    row = (await conn.execute(select(r.c.error, r.c.failure_cause).where(r.c.id == run_id))).one()
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
    reply = await _last_reply(conn, run_id)
    lines = [reply.strip() if reply else "(it wrote no message in its thread)"]
    refs = await _refs(conn, run_id, wait_id)
    lines += ["", *refs] if refs else []
    body = "\n".join(lines)
    agent = f"Decision #{wait_id} (wait) woke: run #{run_id} of @{handle}, which you asked, answered:\n\n{body}"
    thread = f"@{handle} answered (run #{run_id}):\n\n{body}"
    return Answer(runs.clip(agent, runs.MAX_ANSWER_BYTES), runs.clip(thread, ANSWER_IN_THREAD))


def _waits(run_id: int):
    """The waits the end of run ``run_id`` answers: the one it carries (``spawned_wait_id``) and each open wait of an
    ask that run, or a run it goes on from in its session, took in its inbox (``decisions.awaits_run_id``)."""
    r, d = tables.runs, tables.decisions
    chain = select(r.c.id, r.c.resume_of_run_id).where(r.c.id == run_id).cte("answering", recursive=True)
    earlier = r.alias("earlier")
    chain = chain.union(
        select(earlier.c.id, earlier.c.resume_of_run_id).join_from(
            earlier, chain, earlier.c.id == chain.c.resume_of_run_id
        )
    )
    carried = select(r.c.spawned_wait_id).where(r.c.id == run_id).scalar_subquery()
    awaited = d.c.awaits_run_id.in_(select(chain.c.id))
    query = select(d.c.id, d.c.project_id, d.c.plan_id).where(d.c.state == "open", (d.c.id == carried) | awaited)
    return query.order_by(d.c.id)


async def answer_asker(conn: AsyncConnection, run_id: int, state: str, handle: str) -> int | None:
    """When run ``run_id`` of agent ``handle``, asked by another agent's run, ended in ``state``: each wait it answers
    (``_waits``) answered with ``delegated_answer``, in the caller's transaction, and the answer said where the hub's
    lines of the ask go (``work_chats.line_place``); the run that takes the first answer, None when nothing waits for
    it."""
    asked = await chat_run(conn, run_id)
    there = None if asked is None else (asked.chat_id, asked.root_id)
    woke: list[int] = []
    for wait in (await conn.execute(_waits(run_id))).all():
        answer = await delegated_answer(conn, run_id, state, wait.id, handle)
        asker = await run_waits.wake(conn, wait.id, wait.project_id, wait.plan_id, answer.agent)
        if asker is None:
            continue
        found = await chat_run(conn, asker)
        if found is not None and asker not in woke:  # one line an asker, however often it asked
            await work_chats.say(conn, asker, (found.chat_id, found.root_id), answer.thread, there)
        log.info("asked agent answered", extra={"run_id": run_id, "decision_id": wait.id, "asker": asker})
        woke.append(asker)
    return woke[0] if woke else None


# Forwarding


class AgentForwardIn(BaseModel):
    run_id: int = Field(gt=0, description="the run whose last answer is forwarded: a run of the same trace")


class AgentForwarded(BaseModel):
    message_id: int | None = Field(description="the message forwarded into the run's thread; null when refused")
    refusal: str | None = Field(description="why the hub forwarded nothing, as the thread says too")


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
    r = tables.runs
    state = (await conn.execute(select(r.c.state).where(r.c.id == child_id))).scalar_one()
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
    r = tables.runs
    if refusal is None:
        reply = await last_reply(conn, child_id)
        if reply is None:
            refusal = f"run #{child_id} wrote no answer to forward"
    if refusal is None:
        project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run.run_id))).scalar_one()
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
        child_profile = (await conn.execute(select(r.c.profile_id).where(r.c.id == child_id))).scalar_one()
        message_id, _ = await insert_message(
            conn, access, reply[1], thread_root_id=chat.root_id, author_profile_id=child_profile, run_id=run.run_id
        )
        k = tables.run_output_links
        carried = select(k.c.output_id).where(k.c.chat_message_id == reply[0]).order_by(k.c.output_id)
        await output_store.link_outputs(conn, message_id, list((await conn.execute(carried)).scalars()))
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


@worker_router.post(
    "/runs/{run_id}/agent/forwards",
    status_code=201,
    response_model=AgentForwarded,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def forward_answer(request: Request, run_id: RunId, body: AgentForwardIn, user: CurrentUser) -> AgentForwarded:
    """Forward the last answer of run ``body.run_id``, a run of the same trace of an agent system, verbatim into the
    thread of a chat run this worker holds (``forward``). A refusal is answered (201, ``refusal``) and said in the
    thread."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise HTTPException(422, f"run {run_id} is a {run.kind} run: only a chat run's agent forwards an answer")
        message_id, refusal = await forward(conn, run, body.run_id)
    log.info("answer forwarded", extra={"run_id": run_id, "forwarded": body.run_id, "refused": bool(refusal)})
    return AgentForwarded(message_id=message_id, refusal=refusal)
