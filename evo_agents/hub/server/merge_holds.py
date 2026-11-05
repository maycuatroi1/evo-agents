"""The hold of a merge check (``merge_check``) whose verdict failed for a reason of the hub's own machinery
(``evo_agents.hub.merge_infra``), and the thread of the project's triage that ends it.

Where the check would fail (a verdict that failed, or the last of ``judge.JUDGE_ATTEMPTS`` judge runs that ended
without one), ``hold`` keeps it judging instead, and the plan run's wait open: the run stays parked, holding no worker,
and nobody asks its owner anything. It writes the facts in the project's triage chat (``triage.asking_thread``; the
same thread again for a later round of the check, ``triage.asking_reply``): the pull request and its head, the plan
run and its wait, the causes, the failure, the judge run, and what the agent does. The thread mentions the project's
default agent, which starts a chat run for the plan run's owner at hop 1, as a failed run's thread does. The check's
verdict keeps the hold (``KEY``: the causes, the failure, the rounds at this head, when it was held, the thread);
nothing else of the check changes, so the merge gate keeps the merge closed and asking for the check again keeps the
wait.

The agent fixes the system (the hub's code by a pull request it merges, a worker set right, the base branch made
green), then says what comes next through POST /v1/worker/runs/{id}/triage/merge-check (``merge_check.triage_check``,
behind ``evo-agents worker triage merge-check``): ``rejudge`` judges the check again from its first Judge
(``released``), and that verdict answers the run's wait; ``return`` hands the failure back to the plan run, for one the
agent found to be the pull request's after all. The route takes the worker token of the worker that holds the chat run
(404 for another), whose thread is a held check's (404 for any other chat run, 409 for a check no longer held), and
whose dispatcher is the plan run's owner (403 for another member).

A mention that made no chat run (no node of the pool had a slot free, since the judge run that failed the check held
one) is made again on a later pass of the merge queue's job (``ask_again``). A hold nobody ended within
``merge_infra.HOLD_HOURS`` is released by the next pass, which says so in the thread. When the project's triage, or the hub's chats, are off, the thread cannot be opened, the plan run is
gone, or the check was held ``merge_infra.TRIAGE_ROUNDS`` times at its head, ``hold`` holds nothing: the failure goes
back to the plan run, whose message says the failure was the hub's (``failed_by``), and asking for the check again
judges it again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, findings, merge_infra, runs, tables
from evo_agents.hub.server import chat_agents, triage
from evo_agents.hub.server.chat_runs import agent_access, chat_run, hub_reply, post_agent
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

KEY = "triage"  # the key of a check's verdict that holds its hold and its thread
ACTIONS = REJUDGE, RETURN = ("rejudge", "return")
ROUND_KEYS = ("first", "second", "paths", "hidden_count")  # what a verdict holds of the judging of this head
REASON_CHARS = 1200
ASKS = 4  # mentions of the agent a hold makes at most while none made a chat run
ASK_EVERY = timedelta(minutes=10)
SKILL = "evo-hub:triaging-failed-runs"
COMMAND = "evo-agents worker triage merge-check"


# The hold in the check's verdict


def state_of(verdict) -> dict:
    """The hold and the thread ``verdict`` keeps; empty when it keeps none."""
    found = verdict.get(KEY) if isinstance(verdict, dict) else None
    return dict(found) if isinstance(found, dict) else {}


def held_at(row) -> datetime | None:
    """When check ``row`` was held for the triage, None when it is not held."""
    at = state_of(row.verdict).get("held_at")
    try:
        return datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        return None


def expired(row, now: datetime) -> bool:
    """Whether the hold of check ``row`` is older than HOLD_HOURS at ``now``."""
    at = held_at(row)
    return at is not None and now - at >= timedelta(hours=merge_infra.HOLD_HOURS)


def failed_by(row) -> list[str]:
    """The hub's own causes of the failure of settled check ``row``, which went back to its plan run unheld."""
    causes = state_of(row.verdict).get("failed_by")
    return [cause for cause in causes if isinstance(cause, str)] if isinstance(causes, list) else []


def thread_of(row) -> triage.Thread | None:
    """The thread of the check's triage, when it had one."""
    state = state_of(row.verdict)
    chat_id, root_id = state.get("chat_id"), state.get("root_id")
    if not isinstance(chat_id, int) or not isinstance(root_id, int):
        return None
    return triage.Thread(chat_id, root_id, chats.chat_link(chat_id, root_id))


def at_new_head(verdict) -> dict | None:
    """What a verdict hands a new head: the findings (``findings.carried``) and the thread, with no round held yet."""
    carried = findings.carried(verdict) or {}
    state = {key: value for key, value in state_of(verdict).items() if key in ("chat_id", "root_id")}
    return {**carried, KEY: state} if state else carried or None


def settled(verdict, causes: list[str]) -> dict | None:
    """``verdict`` once its check settles: its hold ended, and the hub's own causes of a failure kept as ``failed_by``
    (none for a pass, or a failure of the pull request); a verdict that never had a hold stays as it was."""
    state = {key: value for key, value in state_of(verdict).items() if key not in ("held_at", "failed_by")}
    if causes:
        state["failed_by"] = list(causes)
    if not state and not state_of(verdict):
        return verdict
    return {**(verdict or {}), KEY: state}


def released(row, why: str) -> dict:
    """The values that judge check ``row`` again from its first Judge: the hold ended, the judging of this head gone,
    the findings, the rounds and the thread kept."""
    verdict = settled({key: value for key, value in (row.verdict or {}).items() if key not in ROUND_KEYS}, [])
    return {
        "state": "judging",
        "verdict": verdict,
        "risk": None,
        "base_sha": None,
        "judge_run_id": None,
        "judge_key": None,
        "judge_attempts": 0,
        "reason": runs.clip(f"judged again: {why}", REASON_CHARS),
    }


def held_reason(causes: list[str], reason: str) -> str:
    """The reason a held check shows, to the plan run that asks again and on the web."""
    return runs.clip(
        f"held for the hub's triage, since its own machinery failed it ({merge_infra.said(causes)}): {reason}",
        REASON_CHARS,
    )


# The thread


def _facts(row, project: str, owner: str, state: dict, judge_run: int | None) -> list[str]:
    """The message of the thread when the hub holds check ``row``, as hold ``state`` says."""
    what = f"pull request #{row.number} of {row.repo} at {row.head_sha[:12]}"
    plan = f" (plan {row.plan_id})" if row.plan_id else ""
    lines = [
        f"The merge check of {what}{plan} failed for a reason of the hub's own machinery, not of the pull request: "
        f"{merge_infra.said(state['causes'])}.",
        "Its failure: " + runs.clip(" ".join(str(state["reason"]).split()), REASON_CHARS),
        f"Round {state['rounds']} of {merge_infra.TRIAGE_ROUNDS} at this head. Plan run #{row.run_id} waits for the "
        f"verdict on its wait #{row.decision_id}, parked and holding no worker; the hub asks {owner}, its owner, "
        "nothing.",
    ]
    if judge_run is not None:
        base = f"/v1/projects/{project}/runs/{judge_run}"
        lines.append(f"Judge run: /p/{project}/runs/{judge_run}, log: {base}/events")
    if row.web_url:
        lines.append(f"Pull request: {row.web_url}")
    lines += [
        "Fix the system, not the run: read the judge run's log, find the cause in the hub's code, a worker or the base "
        "branch, and fix it (a pull request you merge, a worker set right); a judge runs the code installed on its "
        "worker, never the pull request's.",
        f"Then judge it again with `{COMMAND} {REJUDGE} --reason TEXT`: its verdict wakes the plan run. When the pull "
        f"request's own change caused it after all, hand the failure back to the plan run with `{COMMAND} {RETURN} "
        f"--reason TEXT`. The skill {SKILL} says how (Merge checks).",
        f"If nobody does, the hub judges it again by itself at {state['until']} UTC.",
    ]
    return lines


async def _owner(conn: AsyncConnection, row):
    """(the id and login of the owner of the plan run that asked for check ``row``, its project's name), or None."""
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.dispatched_by, u.c.login, p.c.name)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == row.run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def _ask(conn: AsyncConnection, row, reason: str, causes: list[str], judge_run: int | None) -> dict | None:
    rounds = int(state_of(row.verdict).get("rounds") or 0)
    if rounds >= merge_infra.TRIAGE_ROUNDS or row.run_id is None:
        return None
    found = await _owner(conn, row)
    if found is None:
        return None
    now = datetime.now(UTC)
    until = now + timedelta(hours=merge_infra.HOLD_HOURS)
    state = {"causes": list(causes), "reason": reason, "rounds": rounds + 1, "until": f"{until:%Y-%m-%d %H:%M}"}
    body = "\n".join(_facts(row, found.name, found.login, state, judge_run))
    args = (conn, row.project_id, found.name, found.dispatched_by)
    existing = thread_of(row)
    posted = await triage.asking_reply(*args, existing, body) if existing is not None else None
    posted = posted or await triage.asking_thread(*args, body)
    if posted is None:  # the project's triage, or the hub's chats, are off
        return None
    thread, agent_run = posted
    del state["until"]
    return {
        **state,
        "held_at": now.isoformat(),
        "chat_id": thread.chat_id,
        "root_id": thread.root_id,
        "agent_run_id": agent_run,
        "asks": 1,
        "asked_at": now.isoformat(),
    }


async def hold(conn: AsyncConnection, row, reason: str, causes: list[str], judge_run: int | None) -> dict | None:
    """The hold of check ``row``, which failed with ``reason`` for the hub's own ``causes``, once its thread asks the
    project's agent; None when the hub holds nothing (see the module), in a savepoint: the failure then goes back to
    the plan run."""
    try:
        async with conn.begin_nested():
            state = await _ask(conn, row, reason, causes, judge_run)
    except Exception as exc:  # a hold is a help: the plan run gets the failure without it
        log.warning(
            "merge check not held for triage", extra={"check_id": row.id, "error": f"{type(exc).__name__}: {exc}"}
        )
        return None
    if state is not None:
        log.info(
            "merge check held for triage",
            extra={
                "check_id": row.id,
                "causes": causes,
                "rounds": state["rounds"],
                "agent_run_id": state["agent_run_id"],
            },
        )
    return state


async def ask_again(conn: AsyncConnection, row, now: datetime) -> dict | None:
    """The hold of check ``row`` once the hub mentioned the project's agent again in its thread, when no mention made
    a chat run yet (none of the pool's nodes had a slot free: the judge run that failed the check held one), ASK_EVERY
    apart and ASKS times in all; None when it is not due."""
    state = state_of(row.verdict)
    asks = int(state.get("asks") or 1)
    if state.get("agent_run_id") is not None or asks >= ASKS or held_at(row) is None:
        return None
    try:
        asked = datetime.fromisoformat(str(state.get("asked_at") or state.get("held_at")))
    except ValueError:
        return None
    thread = thread_of(row)
    if now - asked < ASK_EVERY or thread is None:
        return None
    found = await _owner(conn, row)
    if found is None:
        return None
    text = (
        f"No chat run of the agent took this merge check yet: asking again ({asks + 1} of {ASKS}). The first message "
        "of the thread has the facts and the commands."
    )
    try:
        async with conn.begin_nested():
            posted = await triage.asking_reply(conn, row.project_id, found.name, found.dispatched_by, thread, text)
    except Exception as exc:  # the hold stays: the hub judges it again once it is over
        log.warning(
            "merge check thread not asked again", extra={"check_id": row.id, "error": f"{type(exc).__name__}: {exc}"}
        )
        posted = None
    agent_run = posted[1] if posted is not None else None
    return {**state, "asks": asks + 1, "asked_at": now.isoformat(), "agent_run_id": agent_run}


async def tell(conn: AsyncConnection, row, text: str) -> None:
    """A message of the hub in the thread of the check's triage, when it has one, in a savepoint."""
    thread = thread_of(row)
    if thread is None:
        return
    try:
        async with conn.begin_nested():
            await hub_reply(conn, thread.chat_id, thread.root_id, runs.clip(text, REASON_CHARS * 2))
    except Exception as exc:  # the thread is told for the people who read it: the check goes on without it
        log.warning("merge check thread not told", extra={"check_id": row.id, "error": f"{type(exc).__name__}: {exc}"})


# The agent's route


@dataclass(frozen=True)
class Frame:
    """What the agent of a held check's thread acts on: its chat run, the thread, the check and the owner."""

    agent: chat_agents.AgentRun
    chat_run_id: int
    chat_id: int
    root_id: int
    row: Any  # the check's row, locked
    owner: Principal


async def frame(conn: AsyncConnection, user: Principal, run_id: int) -> Frame:
    """The frame of chat run ``run_id`` in the thread of a held check, the check locked (see the module)."""
    agent = await chat_agents.agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if agent.kind == "chat" else None
    if found is None:
        raise HTTPException(
            404,
            f"run {run_id} is a {agent.kind} run: only the agent of a chat run in a merge check's thread acts on it",
        )
    m = tables.merge_checks
    query = (
        select(m)
        .where(
            m.c.verdict[KEY]["chat_id"].as_integer() == found.chat_id,
            m.c.verdict[KEY]["root_id"].as_integer() == found.root_id,
        )
        .with_for_update()
    )
    row = (await conn.execute(query)).first()
    if row is None:
        raise HTTPException(404, f"chat run {run_id} is in no thread the hub opened for a merge check")
    if row.project_id != agent.project_id:
        raise HTTPException(409, f"merge check {row.id} is in another project than the agent of chat run {run_id}")
    r = tables.runs
    owner_id = None
    if row.run_id is not None:
        owner_id = (await conn.execute(select(r.c.dispatched_by).where(r.c.id == row.run_id))).scalar_one_or_none()
    if owner_id != agent.principal.user_id:
        raise HTTPException(
            403,
            f"the plan run of merge check {row.id} belongs to another member than {agent.principal.login}, who "
            f"dispatched chat run {run_id}: the agent acts on a check only for that run's owner",
        )
    if held_at(row) is None:
        raise HTTPException(
            409,
            f"the merge check of pull request #{row.number} of {row.repo} is not held for the triage now: it is "
            f"{row.state}" + (f" ({row.reason})" if row.reason else ""),
        )
    return Frame(agent, run_id, found.chat_id, found.root_id, row, agent.principal)


async def said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The ``Decision:`` line of the thread, in the agent's name."""
    chat = await agent_access(conn, frame.chat_id, frame.agent.profile, frame.owner.user_id)
    if chat is None:
        raise HTTPException(409, f"@{frame.agent.profile.handle} is not in chat {frame.chat_id} any more")
    message_id, _ = await post_agent(
        conn, chat, frame.agent.profile, frame.chat_run_id, runs.clip(text, 4000), frame.root_id
    )
    return message_id
