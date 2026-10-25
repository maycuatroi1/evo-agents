"""Triage and the person, when a work item of the Loop does not get on (plan loop-engine, criterion a7; the report's
section Triage).

The engine counts a failure of a work item (``rules.failure_move``): its run failed or was lost for good, stopped on its
caps, ended without what it was for (an author run with no plan, a plan run with steps not done, a task run with no
version in review), or a merge request of its plan was reverted. A person cancelling the run cancels the work item. A
work item that waits for a person for another reason (a review of its SRS version, the settle chat of an open finding
of level decide, an author's question, a plan run's decision, a merge request held) is ``waiting_human`` and never a
failure. The first failure queues it again; this module does the rest, in a step of each tick after the engine's:

- the second failure (``watching``, ``triage``): the hub tells the triage thread the work item's last run opened (an
  author or a plan run that failed has one, ``triage.tell_thread``) about the work item, its failed runs and its chain;
  a run without one (a task run, a run done without what it was for) gets a thread in the project's triage chat with
  the project's default agent asked (``triage.asking_thread``). The third hand-out waits for that thread's chat run to
  end, at most ``model.TRIAGE_WAIT_MINUTES`` (``rules.triaged_move``); a project whose triage is off has none to wait for.
- from the third failure on (``waiting_human``, ``loop_decision``): a decision of category loop on the work item's last
  run, asked of the person that run stands for (the run_as of a task run's agent type, the owner of loop.worker for an
  author or a plan run), with three options. The hub does the one chosen (``answer_loop``), as it does a release's:
  ``retry`` queues the work item again with its failures counted from 0, ``drop`` cancels it (``dropped``), ``manual``
  cancels it and leaves its chain to a person (``manual``).

Each writes its ledger row (``triaged``, ``asked``) and keeps what it did in the work item's detail (``triage``,
``decision``) with the failure it answers (its count and the work item's attempts), so a tick does it once per
failure. Nothing here calls a model.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import literal

from evo_agents.hub import loop_jobs, runs, tables
from evo_agents.hub.server.loop.model import (
    ASKED,
    CANCELLED,
    CANCELLED_EVENT,
    QUEUED,
    READY,
    TRIAGE_WAIT_MINUTES,
    TRIAGED,
    WAITING_HUMAN,
    WATCHING,
    ItemView,
    View,
)
from evo_agents.hub.server.loop.view import run_chains

log = logging.getLogger(__name__)

OPTIONS = (
    ("retry", "Run it again", "the Loop hands it out again, its failures counted from 0"),
    ("drop", "Drop it", "the work item is cancelled; its chain stays as it is"),
    ("manual", "Leave it to a person", "the work item is cancelled and a person does the work by hand"),
)
ANSWERED = {"drop": "dropped", "manual": "manual"}  # the reason a cancelling answer leaves
SHOWN_FAILURES = 5


def _line(text) -> str:
    return " ".join(str(text or "").split())


async def _project(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return await conn.scalar(select(p.c.name).where(p.c.id == project_id))


async def _failures(conn: AsyncConnection, item_id: int) -> list[dict]:
    """The details of the work item's ``failed`` ledger rows, oldest first."""
    e = tables.loop_item_events
    query = select(e.c.detail).where(e.c.item_id == item_id, e.c.kind == "failed").order_by(e.c.id)
    return [dict(detail or {}) for (detail,) in await conn.execute(query)]


async def last_run(conn: AsyncConnection, item: ItemView) -> int | None:
    """The newest run of the work item: the newest that goes on from the last run handed out for it."""
    if not item.runs:
        return None
    chains = await run_chains(conn, [max(item.runs)])
    return chains[max(item.runs)][-1]


async def _stands_for(conn: AsyncConnection, run_id: int | None, goal_id: int | None) -> int | None:
    """Who run ``run_id`` stands for (its dispatcher); the goal's owner without a run."""
    if run_id is not None:
        return await conn.scalar(select(tables.runs.c.dispatched_by).where(tables.runs.c.id == run_id))
    from evo_agents.hub.server.loop.runs_of import goal_owner

    return await goal_owner(conn, goal_id)


def _chain(item: ItemView, view: View) -> str:
    goal = view.goals.get(item.goal_id) if item.goal_id is not None else None
    claim = view.claims.get(item.claim_id) if item.claim_id is not None else None
    parts = [f"goal {goal.code}" if goal is not None else None]
    parts += [f"claim {claim.code} of canvas {claim.canvas}" if claim is not None else None]
    parts += [f"use case {item.use_case} v{item.srs_version}" if item.use_case else None]
    task = view.tasks.get(item.task_id) if item.task_id is not None else None
    parts += [f"task #{task.number}" if task is not None else None, f"plan {item.plan_id}" if item.plan_id else None]
    return ", ".join(part for part in parts if part) or "no ancestor"


def _facts(item: ItemView, view: View, project: str, failed: list[dict]) -> list[str]:
    lines = [
        f"Work item #{item.id} ({item.kind}, {item.key}) of the Loop of project {project} failed "
        f"{item.failures} times. Its chain: {_chain(item, view)}.",
        "Failures:",
    ]
    for detail in failed[-SHOWN_FAILURES:]:
        run = f"run #{detail['run_id']}" if detail.get("run_id") else "no run"
        why = detail.get("why") or (f"the run ended {detail['run_state']}" if detail.get("run_state") else None)
        why = why or (f"merge requests reverted: {detail['reverted']}" if detail.get("reverted") else "unknown")
        lines.append(f"- {run}: {_line(why)}")
    return lines


async def _keep(conn: AsyncConnection, item: ItemView, state: str, reason: str, key: str, kept: dict) -> bool:
    """Keep ``kept`` as the work item's detail ``key`` while it is still in ``state`` for ``reason``."""
    i = tables.loop_items
    values = {"detail": i.c.detail.op("||")(literal({key: kept}, JSONB)), "updated_at": func.now()}
    query = update(i).where(i.c.id == item.id, i.c.state == state, i.c.reason == reason).values(values)
    return await conn.scalar(query.returning(i.c.id)) is not None


async def _ledger(conn: AsyncConnection, item_id: int, kind: str, state: str, reason, detail, run_id=None) -> None:
    row = {"item_id": item_id, "kind": kind, "state": state, "reason": reason, "detail": dict(detail or {})}
    await conn.execute(insert(tables.loop_item_events).values(**row, run_id=run_id))


async def _triage(conn: AsyncConnection, project_id: int, project: str, item: ItemView, view: View) -> bool:
    """The triage of work item ``item`` at its second failure (see the module)."""
    from evo_agents.hub.server import triage  # it reads runs through the routes that import this

    run_id = await last_run(conn, item)
    lines = _facts(item, view, project, await _failures(conn, item.id))
    lines.append(
        f"The Loop hands it out once more when the agent of this thread is done, within {TRIAGE_WAIT_MINUTES} minutes; "
        "a third failure asks a person what next."
    )
    body, told = "\n".join(lines), None
    try:
        async with conn.begin_nested():
            told = await triage.tell_thread(conn, run_id, body) if run_id is not None else None
            if told is None:
                owner = await _stands_for(conn, run_id, item.goal_id)
                told = await triage.asking_thread(conn, project_id, project, owner, body) if owner else None
    except Exception as exc:  # a thread is a help: the work item waits out its wait without one
        log.warning("loop triage not opened", extra={"item_id": item.id, "error": f"{type(exc).__name__}: {exc}"})
    thread, chat_run = told if told is not None else (None, None)
    kept = {**_round(item), "at": view.now.isoformat(), "thread": thread.link if thread else None}
    kept["run_id"] = chat_run
    if not await _keep(conn, item, WATCHING, "triage", "triage", kept):
        return False
    await _ledger(conn, item.id, TRIAGED, WATCHING, "triage", kept, run_id=chat_run)
    return True


def _options() -> list[dict]:
    return [{"key": key, "label": label, "description": text} for key, label, text in OPTIONS]


async def _ask(conn: AsyncConnection, project_id: int, project: str, item: ItemView, view: View) -> bool:
    """The decision of category loop at the work item's third failure (see the module); False without a run."""
    from evo_agents.hub.server.notifications import decision_link, notify

    run_id = await last_run(conn, item)
    owner = await _stands_for(conn, run_id, item.goal_id) if run_id is not None else None
    if run_id is None or owner is None:
        return False
    lines = _facts(item, view, project, await _failures(conn, item.id))
    question = runs.clip(
        f"Work item #{item.id} of the Loop ({item.kind}, {_chain(item, view)}) failed {item.failures} times: run it "
        "again, drop it, or leave it to a person?",
        runs.MAX_QUESTION_CHARS,
    )
    d = tables.decisions
    values = {"run_id": run_id, "project_id": project_id, "category": runs.LOOP_DECISION_CATEGORY}
    values |= {"question": question, "context": "\n".join(lines), "options": _options()}
    decision_id = await conn.scalar(insert(d).values(**values).returning(d.c.id))
    kept = {"id": decision_id, **_round(item), "run_id": run_id}
    if not await _keep(conn, item, WAITING_HUMAN, "loop_decision", "decision", kept):
        return False
    await _ledger(conn, item.id, ASKED, WAITING_HUMAN, "loop_decision", kept, run_id=run_id)
    await notify(
        conn,
        user_id=owner,
        kind="decision",
        project_id=project_id,
        run_id=run_id,
        decision_id=decision_id,
        title=f"The Loop of {project}: work item #{item.id} failed {item.failures} times",
        body=f"{question}\n\n" + "\n".join(lines[1:]),
        link=decision_link(decision_id),
    )
    return True


def _round(item: ItemView) -> dict:
    """What tells this failure of the work item from an earlier one with the same count (a retry counts from 0)."""
    return {"failures": item.failures, "attempts": item.attempts}


def _due(item: ItemView, key: str) -> bool:
    done = item.detail.get(key)
    return not (isinstance(done, dict) and {name: done.get(name) for name in ("failures", "attempts")} == _round(item))


async def handle(conn: AsyncConnection, project_id: int, view: View) -> Counter:
    """Open the triage and ask the decisions the work items of ``view`` are due (see the module), in the tick's
    transaction: how many of each."""
    report: Counter = Counter()
    project = None
    for item in view.items:
        triage = item.state == WATCHING and item.reason == "triage" and _due(item, "triage")
        ask = item.state == WAITING_HUMAN and item.reason == "loop_decision" and _due(item, "decision")
        if not (triage or ask):
            continue
        project = project or await _project(conn, project_id)
        if triage and await _triage(conn, project_id, project, item, view):
            report["triaged"] += 1
        elif ask and await _ask(conn, project_id, project, item, view):
            report["asked"] += 1
    return report


async def answer_loop(conn: AsyncConnection, user, decision_id: int, option: str) -> None:
    """Do what the answer ``option`` of decision ``decision_id`` of category loop chose, for the work item that asked
    it while it still waits for it; a work item that moved on since takes nothing."""
    i = tables.loop_items
    query = select(i.c.id, i.c.project_id).where(
        i.c.detail["decision"]["id"].astext == str(decision_id),
        i.c.state == WAITING_HUMAN,
        i.c.reason == "loop_decision",
    )
    found = (await conn.execute(query.with_for_update())).first()
    if found is None:
        log.info("loop decision answered for no work item", extra={"decision_id": decision_id, "option": option})
        return
    said = {"decision": decision_id, "answer": option, "by": user.login}
    values: dict = {"updated_at": func.now()}
    if option == "retry":
        values |= {"state": QUEUED, "reason": None, "failures": 0}
        kind, state, reason = READY, QUEUED, None
    else:
        reason = ANSWERED[option]
        values |= {"state": CANCELLED, "reason": reason, "finished_at": func.now()}
        kind, state = CANCELLED_EVENT, CANCELLED
    await conn.execute(update(i).where(i.c.id == found.id).values(values))
    await _ledger(conn, found.id, kind, state, reason, said)
    await loop_jobs.defer_tick(conn, found.project_id)
    log.info("loop decision answered", extra={"item_id": found.id, "decision_id": decision_id, "option": option})


__all__ = ["OPTIONS", "answer_loop", "handle", "last_run"]
