"""The thread of a proposal of the Curator in the project's triage chat, where the project's agent answers it for the
owner, and the route it answers through, behind ``evo-agents worker curator answer``.

A charter whose ``answer_by`` is ``agent`` (``evo_agents.hub.curator.ANSWERERS``) has the hub open a thread for each
proposal a review run records open (``proposals.record_proposal``) and ask again in it when a deferred proposal opens
again (``collect.reopen_deferred``): ``on_open``, in a savepoint of the caller's transaction, so nothing that goes wrong
in it undoes the proposal or its reopening. The thread is the one of ``triage.asking_thread``, in the project's triage
chat ("Triage: <project>"), with the review run's owner, the member the night shift dispatches as, put in it. Its first
message holds the proposal's facts: its tier and why, its summary, paths and evidence, its draft plan, what accepting it
makes, and its links; it names the skill SKILL the agent follows and mentions the project's default agent, which starts
a chat run for the owner at hop 1, as the thread of a failed run does. A proposal has one thread (``proposal_threads``):
once it opens again after a defer, the hub writes in that thread again and mentions the agent once more
(``triage.asking_reply``). An owner who is no admin of the project gets the thread without the mention, since only an
admin's answer counts. With answer_by ``owner`` (the default), the project's triage off or the hub's chats off, the hub
opens no thread and the proposal waits for an admin as before.

POST /v1/worker/runs/{id}/curator/answer takes the worker token of the worker that holds a chat run (404 for another,
as the triage routes do), whose thread is a proposal's (404 for any other chat run), and answers that proposal (403 for
another) for the chat run's dispatcher, the person at the origin of its chain, who must hold the admin role on the
project (403), while the charter still has answer_by ``agent`` (409) and the proposal is open or deferred (409). It
answers through ``proposals.answer_proposal_as``, as an admin's answer on the web does, so accepting a proposal of a tier
the charter's ``planned_tiers`` names makes the Curator's plan. The note, the reason, is required. The audit row
(``curator.proposal``) and the ledger line name the agent and the chat run, the hub posts a ``Decision:`` line in the
thread in the agent's name, and the morning brief lists the answers of the agent (``brief``). The admins' answer route
stays as it is.

When the hub leaves a change of such a proposal open for its owner with a pull request (``changes._reopen``: a tier the
charter's ``auto_merge`` does not name, a sign, CI that failed, K5 refusing), it says so in the proposal's thread with
the reason and the pull request (``on_left_open``, in a savepoint of the caller's transaction), and mentions the agent
when the Judge passed the change and the owner is an admin. POST /v1/worker/runs/{id}/curator/merge, behind
``evo-agents worker curator merge``, then merges that pull request for the owner, in the same frame as an answer (the
thread's proposal, 403 for a change of another; an owner who is no admin, 403; answer_by ``agent``, 409), with the
workers' App at the head the Judge passed, as the hub merges one (``changes.merge_read``, ``changes.merge_at_head``),
only when ``judge.agent_merge_decision`` allows it on what the hub reads from GitHub then: the change left ``open``,
its Judge passed, its head the one judged, every sign of its files one of a protected path, its pull request open
into the default branch and mergeable, the repo's ruleset checked, CI as the charter asks; and when the project's night
shift is not paused and K5 of the chat run's principal lets it (``autonomy_gates``). Anything else is refused, 409 with
the reason (503 while GitHub does not answer). The merge writes what the hub's own does (``changes.record_merge``),
with the agent and the chat run in the audit row, the ledger line and the owner's notice, posts a ``Decision:`` line in
the thread, and the morning brief lists it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, judge, review, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import autonomy_gates, changes, chat_agents, merge_gate, triage
from evo_agents.hub.server.changes import Change
from evo_agents.hub.server.chat_runs import agent_access, chat_run, hub_reply, post_agent
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.proposals import Proposal, ProposalAnswer, answer_proposal_as, planned_tiers_of
from evo_agents.hub.server.protection import github_target
from evo_agents.hub.server.runs.models import LINE, MAX_ID, REFUSALS, RunId
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

SKILL = "answering-curator-proposals"  # the skill of the evo-hub plugin the agent of the thread follows
SUMMARY_CHARS = 1200  # of the proposal's summary in the first message
SHOWN = 10  # paths, pieces of evidence and steps of the draft the first message names; it counts all


class CuratorAnswerIn(BaseModel):
    proposal_id: int = Field(ge=1, le=MAX_ID, description="the proposal of this thread")
    action: Literal[review.ANSWERS]
    note: str = Field(
        min_length=1,
        max_length=review.MAX_NOTE_CHARS,
        pattern=LINE,
        description="why, in one line: the ledger and the Decision: line keep it",
    )
    defer_days: int | None = Field(
        None, ge=review.DEFER_DAYS[0], le=review.DEFER_DAYS[1], description="with defer; 7 by default"
    )

    @model_validator(mode="after")
    def _days(self):
        if self.defer_days is not None and self.action != "defer":
            raise ValueError("defer_days goes with the action defer")
        return self


class CuratorAnswered(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    proposal: Proposal = Field(description="the proposal as answered, with the change accepting it made")


class CuratorMergeIn(BaseModel):
    change_id: int = Field(ge=1, le=MAX_ID, description="the change of this thread's proposal the hub left open")


class CuratorMerged(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    change: Change = Field(description="the change as merged, with its merge commit")


@dataclass(frozen=True)
class Frame:
    """What an answer acts on: the chat run, its thread, the proposal, its project and the owner it answers for."""

    agent: chat_agents.AgentRun
    chat_run_id: int
    chat_id: int
    root_id: int
    proposal_id: int
    project: str
    owner: Principal


# The thread


async def answer_by(conn: AsyncConnection, project_id: int) -> str:
    """Who answers the proposals of the project, as its newest charter says: ``owner`` without one or the key."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    body = (await conn.execute(query)).scalar_one_or_none() or {}
    return "agent" if body.get("answer_by") == "agent" else "owner"


async def thread_of(conn: AsyncConnection, proposal_id: int) -> triage.Thread | None:
    """The thread the hub opened for proposal ``proposal_id``, or None."""
    t = tables.proposal_threads
    query = select(t.c.chat_id, t.c.root_message_id).where(t.c.proposal_id == proposal_id)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return triage.Thread(row.chat_id, row.root_message_id, chats.chat_link(row.chat_id, row.root_message_id))


def _line(text) -> str:
    return " ".join(str(text or "").split())


def _more(items: list, shown: list[str]) -> str:
    return ", ".join(shown) + (f" and {len(items) - len(shown)} more" if len(items) > len(shown) else "")


def _facts(row, planned: tuple[int, ...], owner: str, *, again: bool) -> list[str]:
    """The lines of the thread's message about proposal ``row``: what it is, what it rests on, what accepting it makes,
    its links and how to answer it."""
    title = _line(row.title)
    head = "is open again after its defer" if again else "waits for an answer"
    lines = [f"Curator proposal #{row.id} of {row.project} {head}: {title}"]
    reasons = "; ".join(_line(reason) for reason in row.tier_reasons or [])
    lines.append(f"Tier {row.tier} ({row.kind}, lens {row.lens})" + (f": {reasons}" if reasons else "."))
    if row.summary:
        lines.append("Summary: " + runs.clip(_line(row.summary), SUMMARY_CHARS))
    paths = list(row.paths or [])
    lines.append("Paths: " + (_more(paths, [f"{p['repo']}:{p['path']}" for p in paths[:SHOWN]]) or "none"))
    evidence = list(row.evidence or [])
    cited = _more(evidence, [review.evidence_text(item) for item in evidence[:SHOWN]]) or "none of its own"
    findings = ", ".join(f"#{finding}" for finding in row.finding_ids or [])
    lines.append(
        f"Evidence, {row.evidence_count} pieces with its findings': {cited}"
        + (f"; findings {findings}" if findings else "")
        + "."
    )
    steps = [step for step in (row.draft or {}).get("steps") or [] if isinstance(step, dict)]
    named = [f"{step.get('id')} {_line(step.get('title'))}" for step in steps[:SHOWN]]
    lines.append(f"Draft plan {(row.draft or {}).get('id')}: {_more(steps, named) or 'no step'}.")
    if row.promotion is not None:
        lines.append("Accepting it promotes agents' memories into a shared memory of the project, and makes no plan.")
    elif row.tier in planned:
        lines.append(
            f"Accepting it makes its draft the Curator's plan, which the night shift builds (the charter plans "
            f"{judge.tier_words(planned)})."
        )
    else:
        lines.append(
            f"Accepting it records the answer alone: the charter plans {judge.tier_words(planned)}, and the owner "
            f"makes a change of tier {row.tier}."
        )
    base = f"/v1/projects/{row.project}/curator/proposals/{row.id}"
    lines.append(f"Proposal: /p/{row.project}/curator/proposals/{row.id}, API: {base}, ledger: {base}/ledger")
    lines.append(
        f"Answer it for {owner} with `evo-agents worker curator answer {row.id} accept|reject|defer --note TEXT`, as "
        f"the skill {SKILL} says: check the evidence and the cause before you trust them."
    )
    return lines


async def _proposal_row(conn: AsyncConnection, proposal_id: int):
    p, r, pr, u = tables.proposals, tables.runs, tables.projects, tables.users
    query = (
        select(
            p.c.id,
            p.c.project_id,
            pr.c.name.label("project"),
            p.c.state,
            p.c.title,
            p.c.tier,
            p.c.kind,
            p.c.lens,
            p.c.tier_reasons,
            p.c.summary,
            p.c.paths,
            p.c.evidence,
            p.c.evidence_count,
            p.c.finding_ids,
            p.c.draft,
            p.c.promotion,
            r.c.dispatched_by.label("owner_id"),
            u.c.login.label("owner"),
        )
        .join_from(p, pr, pr.c.id == p.c.project_id)
        .join(r, r.c.id == p.c.run_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(p.c.id == proposal_id)
    )
    return (await conn.execute(query)).one()


async def _role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def _post(conn: AsyncConnection, row, existing: triage.Thread | None, body: str, *, ask: bool):
    """(the thread, the chat run its mention reached) once the hub wrote ``body`` in the proposal's thread, a new one
    when it has none or its thread is no longer in the project's triage chat; the agent mentioned with ``ask``. None
    while the project's triage, or the hub's chats, are off."""
    args = (conn, row.project_id, row.project, row.owner_id)
    if not ask:
        if existing is not None:
            await hub_reply(conn, existing.chat_id, existing.root_id, body)
            return existing, None
        thread = await triage.project_thread(*args, body)
        return None if thread is None else (thread, None)
    asked = await triage.asking_reply(*args, existing, body) if existing is not None else None
    return asked or await triage.asking_thread(*args, body)


async def _keep(conn: AsyncConnection, proposal_id: int, thread: triage.Thread, agent_run: int | None) -> None:
    """Keep ``thread`` as the proposal's, with the chat run its latest mention made."""
    t = tables.proposal_threads
    values = {"chat_id": thread.chat_id, "root_message_id": thread.root_id, "agent_run_id": agent_run}
    statement = pg_insert(t).values(proposal_id=proposal_id, **values)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[t.c.proposal_id], set_={**values, "asked_at": func.now()})
    )


async def _ask(conn: AsyncConnection, proposal_id: int, *, again: bool) -> triage.Thread | None:
    row = await _proposal_row(conn, proposal_id)
    if row.state != "open" or await answer_by(conn, row.project_id) != "agent":
        return None
    lines = _facts(row, await planned_tiers_of(conn, row.project_id), row.owner, again=again)
    role = await _role(conn, row.owner_id, row.project_id)
    admin = has_role(role, "admin")
    if not admin:
        lines.append(
            f"{row.owner}, whom the night shift runs as, holds {role or 'no role'} on {row.project}, not admin: the hub "
            "asks no agent, since only an admin's answer counts. An admin answers it on the web."
        )
    existing = await thread_of(conn, proposal_id)
    posted = await _post(conn, row, existing, "\n".join(lines), ask=admin)
    if posted is None:
        return None
    thread, agent_run = posted
    await _keep(conn, proposal_id, thread, agent_run)
    log.info(
        "curator thread opened",
        extra={"proposal_id": proposal_id, "chat_id": thread.chat_id, "agent_run_id": agent_run, "again": again},
    )
    return thread


async def on_open(conn: AsyncConnection, proposal_id: int, *, again: bool = False) -> triage.Thread | None:
    """The thread of proposal ``proposal_id``, just recorded open or open ``again`` after a defer, with the project's
    agent asked to answer it, when the charter has the agent answer; None otherwise or when it cannot be opened, in a
    savepoint: nothing inside it undoes the proposal."""
    try:
        async with conn.begin_nested():
            return await _ask(conn, proposal_id, again=again)
    except Exception as exc:  # a thread is a help: the proposal waits for an admin without it
        log.warning(
            "curator thread not opened", extra={"proposal_id": proposal_id, "error": f"{type(exc).__name__}: {exc}"}
        )
        return None


def _left_lines(change, row, *, ask: bool, role: str | None) -> list[str]:
    """The lines of the thread's message about ``change``, which the hub left open for its owner."""
    head = change.head_sha[:12] if change.head_sha else "(none)"
    lines = [
        f"Change #{change.id} of Curator proposal #{row.id} of {row.project} (tier {change.tier}) is left open for its "
        f"owner: {_line(change.reason) or 'no reason given'}",
        f"Pull request: {change.pr_url}, head {head}, into {change.base_branch}. The Judge "
        + ("passed it." if change.passed else "did not pass it."),
    ]
    if ask:
        lines.append(
            f"Merge it for {row.owner} with `evo-agents worker curator merge {change.id}` once you have read its diff "
            f"at {head}, as the skill {SKILL} says. The hub merges it only at the head the Judge passed, with no sign "
            "but of protected paths, open into the default branch, and with K5 letting it; it refuses anything else "
            "with the reason."
        )
    elif not change.passed:
        lines.append("A change its Judge did not pass is not the agent's to merge: its owner decides on GitHub.")
    else:
        lines.append(
            f"{row.owner} holds {role or 'no role'} on {row.project}, not admin: the hub asks no agent; an admin of "
            "the project merges it on GitHub."
        )
    return lines


async def _left(conn: AsyncConnection, change_id: int) -> triage.Thread | None:
    c, p = tables.curator_changes, tables.proposals
    query = select(c, p.c.run_id.label("review_run")).join_from(c, p, p.c.id == c.c.proposal_id)
    change = (await conn.execute(query.where(c.c.id == change_id))).one()
    if change.state != "open" or change.pr_url is None or change.review_run is None:
        return None
    if await answer_by(conn, change.project_id) != "agent":
        return None
    row = await _proposal_row(conn, change.proposal_id)
    role = await _role(conn, row.owner_id, row.project_id)
    ask = bool(change.passed) and has_role(role, "admin")
    existing = await thread_of(conn, change.proposal_id)
    posted = await _post(conn, row, existing, "\n".join(_left_lines(change, row, ask=ask, role=role)), ask=ask)
    if posted is None:
        return None
    thread, agent_run = posted
    if existing is None or agent_run is not None:
        await _keep(conn, change.proposal_id, thread, agent_run)
    log.info(
        "curator thread told of a change left open",
        extra={"change_id": change_id, "proposal_id": change.proposal_id, "agent_run_id": agent_run},
    )
    return thread


async def on_left_open(conn: AsyncConnection, change_id: int) -> triage.Thread | None:
    """The thread of the proposal of change ``change_id``, which the hub just left open for its owner with a pull
    request, told so with the reason and the pull request, and the agent asked to merge it when its Judge passed it;
    None when the charter has its admins answer or nothing is written, in a savepoint: nothing inside it undoes the
    change's move."""
    try:
        async with conn.begin_nested():
            return await _left(conn, change_id)
    except Exception as exc:  # a thread is a help: the change waits for its owner without it
        log.warning("curator thread not told", extra={"change_id": change_id, "error": f"{type(exc).__name__}: {exc}"})
        return None


# The answer


async def _frame(conn: AsyncConnection, user: Principal, run_id: int, proposal_id: int | None) -> Frame:
    """The frame of chat run ``run_id`` in a proposal's thread; with ``proposal_id``, 403 for another proposal."""
    agent = await chat_agents.agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if agent.kind == "chat" else None
    if found is None:
        raise HTTPException(
            404, f"run {run_id} is a {agent.kind} run: only the agent of a chat run in a proposal's thread answers it"
        )
    t, p, pr = tables.proposal_threads, tables.proposals, tables.projects
    query = select(t.c.proposal_id).where(t.c.chat_id == found.chat_id, t.c.root_message_id == found.root_id)
    held = (await conn.execute(query)).scalar_one_or_none()
    if held is None:
        raise HTTPException(404, f"chat run {run_id} is in no thread the hub opened for a proposal of the Curator")
    if proposal_id is not None and held != proposal_id:
        raise HTTPException(
            403, f"chat run {run_id} is in the thread of proposal {held}: its agent answers that proposal alone"
        )
    query = select(p.c.project_id, pr.c.name).join_from(p, pr, pr.c.id == p.c.project_id).where(p.c.id == held)
    row = (await conn.execute(query)).one()
    if row.project_id != agent.project_id:
        raise HTTPException(409, f"proposal {held} is in another project than the agent of chat run {run_id}")
    owner = agent.principal
    access = await project_access(conn, owner, row.name)
    if not has_role(access.role, "admin"):
        raise HTTPException(
            403,
            f"{owner.login}, who dispatched chat run {run_id}, holds {access.role or 'no role'} on project {row.name}: "
            "the agent answers a proposal only for an admin of the project",
        )
    if await answer_by(conn, row.project_id) != "agent":
        raise HTTPException(
            409, f"the charter of project {row.name} has answer_by owner now: its admins answer proposal {held}"
        )
    return Frame(agent, run_id, found.chat_id, found.root_id, held, row.name, owner)


async def _said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The line of the thread, in the agent's name; it opens with ``Decision:``."""
    chat = await agent_access(conn, frame.chat_id, frame.agent.profile, frame.owner.user_id)
    if chat is None:
        raise HTTPException(409, f"@{frame.agent.profile.handle} is not in chat {frame.chat_id} any more")
    message_id, _ = await post_agent(
        conn, chat, frame.agent.profile, frame.chat_run_id, runs.clip(text, 4000), frame.root_id
    )
    return message_id


def decision_line(proposal: Proposal, body: CuratorAnswerIn, owner: str) -> str:
    """The ``Decision:`` line of an answer: what the agent answered, for whom and why, and what it made."""
    line = f"Decision: {review.ANSWERED[body.action]} proposal #{proposal.id} (tier {proposal.tier}) for {owner}: "
    line += _line(body.note)
    if proposal.deferred_until is not None:
        line += f" It opens again at {proposal.deferred_until:%Y-%m-%d %H:%M} UTC."
    change = proposal.change
    if change is not None:
        made = f"plan {change.plan_id}" if change.plan_id else (change.reason or "no plan")
        line += f" Change #{change.id}: {change.state}, {made}."
    elif body.action == "accept":
        line += " It makes no plan."
    return line


@worker_router.post("/runs/{run_id}/curator/answer", response_model=CuratorAnswered, responses={**REFUSALS})
async def answer(request: Request, run_id: RunId, body: CuratorAnswerIn, user: CurrentUser) -> CuratorAnswered:
    """Accept, reject or defer the proposal of this thread for the chat run's dispatcher, an admin of the project, as
    their answer on the web does; 403 for another proposal or a dispatcher who is no admin, 409 for a proposal neither
    open nor deferred or a charter that has its admins answer."""
    async with request.app.state.engine.begin() as conn:
        frame = await _frame(conn, user, run_id, body.proposal_id)
        said = ProposalAnswer(action=body.action, note=body.note, defer_days=body.defer_days)
        proposal = await answer_proposal_as(
            conn, frame.owner, frame.project, frame.proposal_id, said, agent=(frame.agent.profile.handle, run_id)
        )
        message_id = await _said(conn, frame, decision_line(proposal, body, frame.owner.login))
    log.info(
        "proposal answered by the agent",
        extra={"proposal_id": frame.proposal_id, "action": body.action, "agent_run_id": run_id},
    )
    return CuratorAnswered(message_id=message_id, proposal=proposal)


# The merge


def _not_left(change) -> str | None:
    """Why the agent may not merge ``change``, from the hub's own record of it; None when it may try."""
    if change.state != "open":
        return f"it is {change.state}: the agent merges only a change the hub left open for its owner"
    if change.forge != "github" or change.pr_number is None or github_target(change.origin) is None:
        return "it has no pull request on GitHub, and the hub merges no other"
    if not change.passed:
        failures = "; ".join((change.verdict or {}).get("failures") or []) or "no verdict"
        return f"the Judge did not pass it: {failures}"
    if not judge.is_sha(change.head_sha):
        return "it names no head the Judge passed"
    return None


async def _left_change(conn: AsyncConnection, frame: Frame, change_id: int):
    change = await changes.change_with_origin(conn, frame.agent.project_id, change_id)
    if change is None:
        raise HTTPException(404, f"no change #{change_id} of the Curator in project {frame.project}")
    if change.proposal_id != frame.proposal_id:
        raise HTTPException(
            403,
            f"change #{change_id} is of proposal {change.proposal_id}, and chat run {frame.chat_run_id} is in the "
            f"thread of proposal {frame.proposal_id}: its agent merges that proposal's change alone",
        )
    why = _not_left(change)
    if why is not None:
        raise HTTPException(409, f"change #{change_id} is not merged: {why}")
    if await changes.night_paused(conn, change.project_id):
        raise HTTPException(409, f"change #{change_id} is not merged: {changes.PAUSED}")
    if await agent_access(conn, frame.chat_id, frame.agent.profile, frame.owner.user_id) is None:
        raise HTTPException(409, f"@{frame.agent.profile.handle} is not in chat {frame.chat_id} any more")
    return change


def merge_line(change: Change, owner: str) -> str:
    """The ``Decision:`` line of a merge: what the agent merged, for whom, at which head and as which commit."""
    return (
        f"Decision: merged pull request #{change.pr_number} of {change.repo} (change #{change.id}, proposal "
        f"#{change.proposal_id}, tier {change.tier}) for {owner} at {change.head_sha}, as {change.merge_sha}."
    )


async def _merge_now(
    engine, app, frame: Frame, change, read: changes.MergeRead
) -> tuple[str | None, int, Change | None]:
    """Merge ``change`` once the gates let it, under a lock on it: (why not, or None; the Decision: line; the change).
    A refusal commits what the gate K5 wrote (its ask, its audit row and its ledger line)."""
    async with engine.begin() as conn:
        locked = await changes.change_with_origin(conn, frame.agent.project_id, change.id, lock=True)
        moved = locked is None or _not_left(locked) is not None or locked.head_sha != change.head_sha
        if moved:
            return f"change #{change.id} moved while the hub read GitHub: run the command again", 0, None
        if await changes.night_paused(conn, change.project_id):  # paused while the hub read GitHub
            return changes.PAUSED, 0, None
        act = changes.merge_act(change, read)
        said = await autonomy_gates.check(conn, frame.chat_run_id, "K5", **act)
        if not said.act:
            return said.reason or "K5 of the chat run's principal does not let it", 0, None
        try:
            merged = await changes.merge_at_head(app, change, read.facts.tier)
        except GitHubRefused as exc:
            return f"GitHub refused the merge: {exc}", 0, None
        handle = f"@{frame.agent.profile.handle}"
        by = changes.MergedBy(handle, frame.chat_run_id, frame.owner.user_id, frame.owner.login)
        await changes.record_merge(conn, change, merged, read, act, by)
        view = await changes.change_view(conn, change.id)
        message_id = await _said(conn, frame, merge_line(view, frame.owner.login))
    return None, message_id, view


@worker_router.post(
    "/runs/{run_id}/curator/merge",
    response_model=CuratorMerged,
    responses={**REFUSALS, 503: {"model": ErrorBody}},
)
async def merge(request: Request, run_id: RunId, body: CuratorMergeIn, user: CurrentUser) -> CuratorMerged:
    """Merge the pull request of a change of this thread's proposal that the hub left open, for the chat run's
    dispatcher, an admin of the project, at the head the Judge passed, with the workers' App: 403 for a change of
    another proposal, 409 with the reason for anything the merge does not allow, 503 while GitHub does not answer."""
    state = request.app.state
    async with state.engine.begin() as conn:
        frame = await _frame(conn, user, run_id, None)
        change = await _left_change(conn, frame, body.change_id)
        charter = await merge_gate.charter_of(conn, change.project_id)
        plan = await merge_gate.plan_body(conn, change.project_id, change.plan_id) or {}
    if state.github_app is None:
        raise HTTPException(409, f"change #{change.id} is not merged: this hub has no GitHub App of the workers")
    try:
        read = await changes.merge_read(state.engine, state.github_app, state.curator_app, change, charter, plan)
        decided, why = judge.agent_merge_decision(read.facts)
        if decided != "merge":
            raise HTTPException(409, f"change #{change.id} is not merged: {'; '.join(why)}")
        refused, message_id, view = await _merge_now(state.engine, state.github_app, frame, change, read)
    except GitHubUnavailable as exc:
        raise HTTPException(503, f"GitHub did not answer, so change #{change.id} is not merged: {exc}") from None
    except GitHubRefused as exc:
        raise HTTPException(409, f"change #{change.id} is not merged: GitHub refused to read it: {exc}") from None
    if refused is not None:
        raise HTTPException(409, f"change #{change.id} is not merged: {refused}")
    log.info(
        "curator change merged by the agent",
        extra={"change_id": change.id, "sha": view.merge_sha, "agent_run_id": run_id},
    )
    return CuratorMerged(message_id=message_id, change=view)
