"""Local runs (``evo_agents.hub.server.local_runs``): the member's own run of a session, its registration, state,
events, decisions and inbox, each in one transaction, and the reaper that ends the silent ones."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import audit, project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.decisions.models import AnswerIn, Decision
from evo_agents.hub.server.decisions.service.answers import answer_target
from evo_agents.hub.server.decisions.service.asks import store_decision
from evo_agents.hub.server.decisions.service.reads import decision_view
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import write_event, write_state
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.models.local_runs import (
    MAX_TITLE_CHARS,
    POLL_SECONDS,
    LocalDecisionAsk,
    LocalRegister,
    LocalState,
)
from evo_agents.hub.server.runs.repository import local_runs as repository
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.views import current_plan, run_view, visible_plans
from evo_agents.hub.server.security import WEB, Principal

log = logging.getLogger("evo_agents.hub.server.local_runs")


# The run of the member's session


async def owned_local_run(conn: AsyncConnection, user: Principal, project: str, run_id: int, *, write: bool = True):
    """(access, row) of local run ``run_id`` of ``project`` that ``user`` owns, its row locked: state, event_seq,
    events_acked, project_id, name (the project's), plan_id, plan_revision, step_key, dispatched_by and kind. 404 for
    any other run, as for none; 403 without the writer role (``write``) or any grant."""
    access = await project_access(conn, user, project)
    plan_service.reader(access)
    if write:
        check_dispatcher(access)
    row = await repository.owned_row(conn, access.project_id, run_id)
    if row is None or row.dispatched_by != user.user_id:
        raise HTTPException(404, f"project {project} has no local run {run_id} of yours")
    return access, row


async def writing_run(conn: AsyncConnection, access: ProjectAccess, user: Principal, run_id: int) -> int:
    """``run_id`` when it is a local run of the project that ``user`` owns, its row locked, for a plan write that names
    it; 404 for any other run (``plans``)."""
    row = await repository.writing_row(conn, access.project_id, run_id)
    if row is None or row.dispatched_by != user.user_id:
        raise HTTPException(404, f"project {access.name} has no local run {run_id} of yours to record the write on")
    return run_id


async def note_progress(conn: AsyncConnection, run_id: int, plan_id: str, revision: int, step: str | None) -> None:
    """Record on local run ``run_id`` (locked by ``writing_run``) the plan, revision and step its session just wrote.
    The step stays when the plan is the one the run has and the write names none."""
    held = await repository.held_plan(conn, run_id)
    key = step if step is not None else (held.step_key if held.plan_id == plan_id else None)
    values = {"plan_id": plan_id, "plan_revision": revision, "step_key": key, "last_activity_at": func.now()}
    await repository.set_run(conn, run_id, values)


async def _end(conn: AsyncConnection, run_id: int, old: str, actor: str, reason: str, decisions: str) -> None:
    """End local run ``run_id`` (in ``old``, its row locked): its open decisions become ``decisions``."""
    await repository.end_run(conn, run_id, decisions)
    await write_state(conn, run_id, old, "ended", actor, reason)


def _new_run(user: Principal, access: ProjectAccess, session_id: str, title: str, fields: dict) -> dict:
    return {
        "project_id": access.project_id,
        "kind": "local",
        "title": title,
        "dispatched_by": user.user_id,
        "dispatched_via": "web" if user.kind == WEB else "machine",
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "interactive",
        "approval": "auto",
        "timeout_s": 86400,  # runs.timeout_s is at most a day; nothing times a local run out
        "state": "running",
        "session_id": session_id,
        "last_activity_at": func.now(),
        **fields,
    }


async def _register(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, session_id: str, body: LocalRegister
):
    """(run id, created) of the local run of ``session_id``: made now, or the member's own one updated; 409 in an
    archived project (``project_archive``), which takes no local run either."""
    await project_archive.refuse_archived(conn, access.project_id, "it takes no local run of a session")
    title = body.title or runs.clip(f"{body.machine}: {body.cwd}", MAX_TITLE_CHARS)
    fields = {
        "machine": body.machine,
        "cwd": body.cwd,
        "claude_email": body.claude_email,
        "claude_profile": body.claude_profile,
    }
    created = await repository.insert_local(conn, _new_run(user, access, session_id, title, fields))
    if created is not None:
        await write_state(conn, created, "none", "running", "session", f"{body.machine} registered the session")
        return created, True
    found = await repository.held_session(conn, access.project_id, session_id)
    if found.dispatched_by != user.user_id:
        raise HTTPException(403, f"session {session_id} is registered by another member of project {access.name}")
    values = {**fields, "last_activity_at": func.now()}
    if body.title is not None:
        values["title"] = body.title
    if found.state == "ended":  # the session is back
        values |= {"state": "running", "finished_at": None}
    await repository.set_run(conn, found.id, values)
    if found.state == "ended":
        await write_state(conn, found.id, "ended", "running", "session", f"{body.machine} registered the session again")
    return found.id, False


async def register(
    engine: AsyncEngine, user: Principal, project: str, session_id: str, body: LocalRegister
) -> tuple[Run, bool]:
    """(the run of session ``session_id``, whether it is new): made, or the caller's own updated and running again."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        plan_service.reader(access)
        check_dispatcher(access)
        run_id, created = await _register(conn, user, access, session_id, body)
        view = await run_view(conn, run_id)
    if created:
        log.info("local run registered", extra={"run_id": run_id, "login": user.login, "machine": body.machine})
    return view, created


async def _plan_of(conn: AsyncConnection, access: ProjectAccess, plan_id: str, key: str | None):
    """(revision, step key) of the plan the session works on, as the hub holds it; 422 for a plan the caller cannot
    read here or a step it does not have."""
    if plan_id not in await visible_plans(conn, access, None):
        raise HTTPException(422, f"project {access.name} has no plan {plan_id} you can read on the hub")
    held = await current_plan(conn, access.project_id, plan_id)
    if held is None:
        raise HTTPException(422, f"project {access.name} has no plan {plan_id} on the hub")
    if key is not None:
        try:
            index = step_index(held.body, key)
        except PlanProblem as exc:
            raise HTTPException(422, str(exc)) from None
        key = str(held.body["steps"][index].get("id", key)) if isinstance(held.body["steps"][index], dict) else key
    return held.revision, key


async def _move_state(conn: AsyncConnection, run_id: int, row, new: str, values: dict) -> None:
    """Run ``run_id`` (``row``) takes ``values`` and goes to state ``new``, its log saying so."""
    if new == "ended":
        await repository.set_run(conn, run_id, values)
        await _end(conn, run_id, row.state, "session", "the session ended", "cancelled")
        return
    values["state"] = new
    if new != "waiting" or row.state != "waiting":
        values["waiting_since"] = func.now() if new == "waiting" else None
    await repository.set_run(conn, run_id, values)
    if new != row.state:
        await write_state(conn, run_id, row.state, new, "session", f"the session is {new}")


async def report_state(engine: AsyncEngine, user: Principal, project: str, run_id: int, body: LocalState) -> Run:
    """The run after the session's report of its state, plan, step and title; 409 for a run that ended."""
    async with UnitOfWork(engine) as conn:
        access, row = await owned_local_run(conn, user, project, run_id)
        if row.state == "ended":
            raise HTTPException(
                409, f"local run {run_id} ended: register the session again with PUT .../local-runs/<session id>"
            )
        values: dict = {"last_activity_at": func.now()}
        if body.title is not None:
            values["title"] = body.title
        if body.plan_id is not None:
            revision, key = await _plan_of(conn, access, body.plan_id, body.step_key)
            kept = row.step_key if row.plan_id == body.plan_id else None
            values |= {"plan_id": body.plan_id, "plan_revision": revision, "step_key": key or kept}
        await _move_state(conn, run_id, row, body.state or row.state, values)
        return await run_view(conn, run_id)


async def post_events(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    run_id: int,
    store: Callable[[AsyncConnection, object], Awaitable],
):
    """What ``store`` (``run_events.store_events`` of the batch, given the connection and the run's locked row) says
    after it stored the session's events."""
    async with UnitOfWork(engine) as conn:
        _, row = await owned_local_run(conn, user, project, run_id)
        await repository.touch(conn, run_id)
        return await store(conn, row)


# Decisions


async def ask(engine: AsyncEngine, user: Principal, project: str, run_id: int, body: LocalDecisionAsk) -> Decision:
    """The decision asked of the member; the run waits while one is open. 409 for a run that ended."""
    async with UnitOfWork(engine) as conn:
        _, row = await owned_local_run(conn, user, project, run_id)
        if row.state == "ended":
            raise HTTPException(409, f"local run {run_id} ended: register the session again to ask a decision")
        if row.state != "waiting":
            waiting = {"state": "waiting", "waiting_since": func.now(), "last_activity_at": func.now()}
            await repository.set_run(conn, run_id, waiting)
            await write_state(conn, run_id, row.state, "waiting", "session", "the session asks a decision")
        else:
            await repository.touch(conn, run_id)
        view = await store_decision(conn, run_id, row, body)
    log.info("local decision asked", extra={"run_id": run_id, "decision_id": view.id, "category": body.category})
    return view


async def _decision_state(conn: AsyncConnection, run_id: int, decision_id: int, project: str) -> str:
    row = await repository.decision_row(conn, run_id, decision_id)
    if row is None:
        raise HTTPException(404, f"local run {run_id} of project {project} has no decision {decision_id}")
    return row.state


async def take_decision(
    engine: AsyncEngine, streams, user: Principal, project: str, run_id: int, decision_id: int, wait_s: int
) -> Decision:
    """The decision as it is now, taken by the machine once it is answered; with ``wait_s`` it waits for the answer,
    woken by ``streams`` (``run_events.RunStreams``), holding no connection meanwhile."""
    streams.start()
    deadline = time.monotonic() + wait_s
    with streams.subscribe(run_id) as wakeup:
        while True:
            async with UnitOfWork(engine) as conn:
                await owned_local_run(conn, user, project, run_id, write=False)
                state = await _decision_state(conn, run_id, decision_id, project)
                left = deadline - time.monotonic()
                if state != "open" or left <= 0:
                    if state == "answered":
                        await repository.mark_taken(conn, run_id, decision_id)
                    return await decision_view(conn, decision_id)
            await wakeup.wait(min(POLL_SECONDS, left))


async def _open_decision(conn: AsyncConnection, run_id: int, decision_id: int, project: str, body: AnswerIn):
    """Decision ``decision_id`` of run ``run_id``, locked, open and with the option ``body`` names: 404, 409 or 422
    otherwise."""
    found = await repository.locked_decision(conn, run_id, decision_id)
    if found is None:
        raise HTTPException(404, f"local run {run_id} of project {project} has no decision {decision_id}")
    if found.state != "open":
        by = f", by {found.login}" if found.login else ""
        raise HTTPException(
            409,
            f"decision {decision_id} is {found.state}{by}, not open: it was answered first, "
            f"read that answer with GET .../local-runs/{run_id}/decisions/{decision_id}",
        )
    if body.option is not None and body.option not in [option["key"] for option in found.options]:
        keys = ", ".join(option["key"] for option in found.options)
        raise HTTPException(422, f"decision {decision_id} has no option {body.option!r}; its options are {keys}")
    return found


async def answered_on_the_spot(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, decision_id: int, body: AnswerIn
) -> Decision:
    """The decision after the person answered it in the session itself: the first answer wins, a later one gets 409."""
    async with UnitOfWork(engine) as conn:
        access, row = await owned_local_run(conn, user, project, run_id)
        found = await _open_decision(conn, run_id, decision_id, project, body)
        await repository.answer(conn, decision_id, user.user_id, body.option, body.text)
        if row.state == "waiting" and await repository.open_decisions(conn, run_id) == 0:
            await repository.set_run(conn, run_id, {"state": "running", "waiting_since": None})
            await write_state(conn, run_id, "waiting", "running", "session", "the person answered on the spot")
        await repository.touch(conn, run_id)
        await write_event(conn, run_id, {"text": f"decision #{decision_id} answered on the machine"})
        target = answer_target(project, row.plan_id, found.step_key, decision_id, run_id, body.option, "local")
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.DECISION_ANSWER,
            target=f"{target} via=local",
            project_id=access.project_id,
        )
        view = await decision_view(conn, decision_id)
    log.info("local decision answered on the spot", extra={"run_id": run_id, "decision_id": decision_id})
    return view


async def take_inbox(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    run_id: int,
    ack: int | None,
    deliver: Callable[[int, int], object],
    undelivered: Callable[[int], object],
) -> list:
    """The inbox rows of the run that still wait, once the ones up to ``ack`` are delivered; ``deliver`` and
    ``undelivered`` build those statements (``run_events``)."""
    async with UnitOfWork(engine) as conn:
        await owned_local_run(conn, user, project, run_id)
        await repository.touch(conn, run_id)
        if ack is not None:
            await repository.run_statement(conn, deliver(run_id, ack))
        return await repository.rows_of(conn, undelivered(run_id))


# The reaper


async def end_silent(engine: AsyncEngine, batch: int = 500, silent_for: int = runs.LOCAL_SILENT_SECONDS) -> int:
    """End the local runs that sent nothing for ``silent_for`` seconds, at most ``batch`` of them, each in a
    transaction of its own that skips a run another holds; their open decisions expire. How many it ended."""
    async with UnitOfWork(engine) as conn:
        due = await repository.silent_runs(conn, silent_for, batch)
    ended = 0
    for run_id in due:
        async with UnitOfWork(engine) as conn:
            found = await repository.lock_silent(conn, run_id, silent_for)
            if found is None:  # it spoke meanwhile, or another reaper has it
                continue
            reason = f"nothing from the session for {silent_for // 3600} h"
            await _end(conn, run_id, found.state, "hub", reason, "expired")
            ended += 1
    if ended:
        log.info("silent local runs ended", extra={"count": ended})
    return ended
