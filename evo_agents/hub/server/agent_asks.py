"""An agent asks an agent of another project (``evo-agents worker agent ask``, ``chat_runs.ASK_COMMAND``): POST
/v1/worker/runs/{run}/agent/asks, and the answer the hub brings back. ``docs/hub.md`` (Agents across projects) is the
design.

The route takes the worker token of the worker that holds a chat run (404 for a run it does not hold, 422 for a run of
another kind). It refuses, with why, before it writes anything: past ``chat_runs.MAX_HOPS`` or to the run's own agent
(``chat_runs.hop_problem``), an agent the person at the origin of the chain cannot see, one that takes no work
(``agent_tasking.work_home``: deleted, or no pool of its own), one that person may not give work
(``agent_tasking.task_refusal``: the writer rule of a mention). A refusal is answered (201, ``refusal``) and said in the
run's thread.

Else, in a savepoint, the hub opens the origin's direct chat with the agent asked (``chat_direct.direct_chat``) and
puts the question there as a message of the asking run's agent, naming the asking run, which starts a thread: the run
it hands (``chat_runs.hand_work``) is of the agent asked, on its pool, dispatched by the origin, at one hop more, and
acts within the asking run's principal and autonomy snapshot (``run_autonomy.of_message``), so each run of a chain has
no more rights than the run before it. A refusal of ``hand_work`` rolls the savepoint back. The new run names the
asking run in ``spawned_by_run_id`` and the wait it opens on the asking run in ``spawned_wait_id`` (a decision of
category wait that wakes at ``run_waits.MAX_WAIT`` at the latest and notifies nobody): the worker's command notes it,
the agent ends its turn, and the run parks as for any wait, holding no worker. The hub says in the asking run's thread
which agent it asked, in which project, and what.

When the run asked ends (``answer_asker``, from ``chat_runs.chat_run_moved``), the hub answers that wait
(``run_waits.wake``), which resumes the asking run in its session, with ``delegated_answer``: the last message of the
agent asked in its thread and the refs of its work (its outputs, its branch and repos; the knowledge graph nodes are
named in its answer, as the question asks), or why it failed, was cancelled or lost. The same text goes in the asking
run's thread. A run asked that goes on in its session (a parked run resumed, an inbox untaken) carries both columns, so
the run that really ends answers. A wait answered already, by its time or by a person, is left as it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, kg_labels, runs, tables
from evo_agents.hub.server import agent_tasking, audit, cross_labels, run_waits
from evo_agents.hub.server.agent_account_mask import mask_run, mask_values
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_agents import REFUSALS, AgentRun, Handle, agent_run
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.chat_runs import Hand, chat_run, hand_work, hub_reply
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

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class AgentAskIn(BaseModel):
    handle: Handle = Field(description="the agent asked, of another project, @ left out")
    body: str = Field(min_length=1, description=f"what it is asked, at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")

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


def question_text(run: AgentRun, project: str, target: Profile, text: str) -> str:
    """The message that asks agent ``target`` in the origin's direct chat with it: what the asking agent wrote, and
    what its answer should hold."""
    asked = (
        f"@{target.handle}, @{run.profile.handle} of project {project} asks this for {run.principal.login} (run "
        f"#{run.run_id}):\n\n{text.strip()}\n\n"
        "Your last message of this turn is the answer it gets: what you found or did, the files, outputs and branch "
        "of your work, and the knowledge graph nodes it rests on (repo:path or repo:path::Symbol)."
    )
    return runs.clip(asked, chats.MAX_MESSAGE_BYTES)


async def _hand(
    conn: AsyncConnection, run: AgentRun, target: Profile, text: str
) -> tuple[int | None, int | None, str | None]:
    """(the run of ``target`` handed, the question's message, None), or (None, None, why none), in a savepoint
    rolled back on a refusal."""
    origin = run.principal
    nested = await conn.begin_nested()
    try:
        chat_id, _ = await direct_chat(conn, origin, target.handle)
        chat = await chat_access(conn, origin, chat_id)
    except HTTPException as exc:
        await nested.rollback()
        return None, None, str(exc.detail)
    message_id, _ = await insert_message(
        conn, chat, text, thread_root_id=None, author_profile_id=run.profile.id, run_id=run.run_id
    )
    author = f"@{run.profile.handle}"
    hand = Hand(chat, message_id, message_id, target, origin.user_id, origin.login, run.via, run.hop + 1, author, text)
    child, why = await hand_work(conn, hand)
    if child is None:
        await nested.rollback()
        return None, None, why
    await nested.commit()
    return child, message_id, None


async def _open_wait(conn: AsyncConnection, user, run: AgentRun, target: Profile, child: int) -> int:
    """The wait of the asking run that the end of run ``child`` answers; its id."""
    _, row = await held_plan_run(conn, user, run.run_id, lock=True, kinds=("chat",))
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
    r = tables.runs
    await conn.execute(update(r).values(spawned_by_run_id=run.run_id, spawned_wait_id=view.id).where(r.c.id == child))
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
        found = await chat_run(conn, run_id)
        assert found is not None  # every chat run starts from a chat message (hand_work)
        said = await mask_values(conn, request.app.state.sealer, run.profile.id, body.body)
        said = await mask_run(conn, request.app.state.sealer, run_id, said)
        target = await by_handle(conn, body.handle)
        refusal = await _refusal(conn, run, target, body.handle)
        asked, shown = AgentAsked(run_id=None, decision_id=None, refusal=refusal), f"@{body.handle}: {refusal}"
        if refusal is None:
            assert target is not None  # _refusal refuses a handle no agent has
            asked, shown = await _put(conn, user, run, target, said, body.handle)
        await hub_reply(conn, found.chat_id, found.root_id, shown)
    made, refused = asked.run_id, bool(asked.refusal)
    log.info("agent asked", extra={"run_id": run_id, "handle": body.handle, "made": made, "refused": refused})
    return asked


async def _put(
    conn: AsyncConnection, user, run: AgentRun, target: Profile, said: str, handle: str
) -> tuple[AgentAsked, str]:
    """Hand the question ``said`` to agent ``target`` (named ``handle``) and open the asking run's wait for its
    answer: what the route answers, and what the asking run's thread shows."""
    child, message_id, refusal = await _hand(conn, run, target, question_text(run, run.profile.project, target, said))
    if child is None or message_id is None:
        return AgentAsked(run_id=None, decision_id=None, refusal=refusal), f"@{handle}: {refusal}"
    decision_id = await _open_wait(conn, user, run, target, child)
    await audit.record(
        conn,
        actor_id=run.principal.user_id,
        token_id=run.principal.token_id,
        action=audit.CHAT_MESSAGE,
        target=f"message:{message_id} run:{run.run_id} asked:{child}",
    )
    shown = f"@{run.profile.handle} asked @{target.handle} of project {target.project} (run #{child}): {said}"
    return AgentAsked(run_id=child, decision_id=decision_id, refusal=None), shown


# The answer


async def _last_reply(conn: AsyncConnection, run_id: int) -> str | None:
    """The last message of the agent of run ``run_id`` in the thread of the message that started it."""
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
    query = select(m.c.body).where(in_thread, m.c.author_profile_id == row.profile_id).order_by(m.c.id.desc())
    return (await conn.execute(query.limit(1))).scalar_one_or_none()


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
    r, d, p = tables.runs, tables.decisions, tables.projects
    asked = select(p.c.name).join_from(r, p, p.c.id == r.c.project_id).where(r.c.id == run_id)
    asking = select(p.c.name).join_from(d, p, p.c.id == d.c.project_id).where(d.c.id == wait_id)
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
        why = row.error or row.failure_cause or "its worker gave no reason"
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


async def answer_asker(conn: AsyncConnection, run_id: int, state: str, handle: str) -> int | None:
    """When run ``run_id`` of agent ``handle``, asked by another agent's run, ended in ``state``: the wait of the
    asking run answered with ``delegated_answer``, in the caller's transaction, and the answer said in its thread; the
    run that takes the answer, None when nothing waits for it."""
    r, d = tables.runs, tables.decisions
    wait_id = (await conn.execute(select(r.c.spawned_wait_id).where(r.c.id == run_id))).scalar_one_or_none()
    if wait_id is None:
        return None
    wait = (await conn.execute(select(d.c.state, d.c.project_id, d.c.plan_id).where(d.c.id == wait_id))).one_or_none()
    if wait is None or wait.state != "open":
        return None
    answer = await delegated_answer(conn, run_id, state, wait_id, handle)
    asker = await run_waits.wake(conn, wait_id, wait.project_id, wait.plan_id, answer.agent)
    if asker is None:
        return None
    found = await chat_run(conn, asker)
    if found is not None:
        await hub_reply(conn, found.chat_id, found.root_id, answer.thread)
    log.info("asked agent answered", extra={"run_id": run_id, "decision_id": wait_id, "asker": asker, "state": state})
    return asker
