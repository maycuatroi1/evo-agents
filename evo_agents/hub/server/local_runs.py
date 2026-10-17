"""Local runs: a Claude Code session on a member's own machine, which the hub watches and asks decisions for.

A local run (``runs.kind = 'local'``) is the record of one session the member's machine registers, so the web, the
Monitor and the run list show what that session does, and a question of its agent reaches the member wherever they
are (the Inbox, Telegram, the web) and the answer goes back to the session. The hub only watches it: it neither
dispatches nor steers it (a cancel, takeover, message or rerun of a local run is 409), no worker holds it, and it has
no lease, no slot and no attempts. It is its member's alone: no other member reads it in any list, log, stream or
decision, whatever their role, and each route below answers 404 to another one. The routes take the member's token
(a machine token from the machine, or a web session) and the writer role on the project.

``PUT /v1/projects/{project}/local-runs/{session_id}`` registers the session (the Claude session id is the key,
unique per project) and is idempotent: it answers 201 for a new run and 200 for one that exists, updates what the
machine says (machine, cwd, Claude account, title) and revives a run that ended. 403 when another member registered
that session id. ``POST .../local-runs/{id}/state`` reports ``running``, ``waiting``, ``idle`` or ``ended``, the plan
and step the session works on, and the title; every route below touches ``last_activity_at``. A run in no state but
``ended`` that sent nothing for ``runs.LOCAL_SILENT_SECONDS`` is ended by the reaper (``end_silent``), and an ended run
registers again with the same PUT. ``POST .../events`` stores the session's log as a worker's batch is stored
(``run_events.store_events``).

``POST .../decisions`` asks the member a decision, with the options of any decision and the category ``question`` too:
the run is ``waiting`` while one is open, the member is notified as for any decision, and answers it on the web, in
Telegram or with ``evo hub decision answer``. The first answer wins, as for every decision (409 for a later one). The
machine takes it by long-poll (``GET .../decisions/{id}?wait_s=N``, at most MAX_WAIT_SECONDS) or by asking again, or by
the run's inbox (``POST .../inbox``, as a worker's). When the person answered on the spot, in the session itself, the
machine reports it (``POST .../decisions/{id}/answered``): that is an answer like the others, and the first of them
wins.

A plan write that names the local run (``run_id`` of PUT and PATCH of ``/v1/projects/{project}/plans``) records it on
the revision (``plan_revisions.run_id``) and sets the run's plan, revision and step.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.decisions import (
    AnswerIn,
    Decision,
    DecisionAsk,
    _answer_target,
    decision_view,
    store_decision,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import (
    BatchRoute,
    EventBatch,
    EventsAck,
    Inbox,
    InboxAck,
    InboxMessage,
    RunStreams,
    _deliver,
    _undelivered,
    store_events,
)
from evo_agents.hub.server.run_state import write_event, write_state
from evo_agents.hub.server.runs.models import LINE, MAX_ID, REFUSALS, Run, RunId
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.views import current_plan, run_view, visible_plans
from evo_agents.hub.server.security import WEB, CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 25  # the longest a long-poll for an answer holds
POLL_SECONDS = 5.0  # a long-poll looks again this often, wakeup or not
MAX_TITLE_CHARS = 200  # runs.title
SESSION_ID = Annotated[str, Path(min_length=1, max_length=200, pattern=LINE, description="the Claude session id")]
DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]

REFUSED = {**REFUSALS, 422: {"model": ErrorBody}}
router = APIRouter(prefix="/v1/projects", tags=["local runs"], responses={401: {"model": ErrorBody}})
batch_router = APIRouter(
    prefix="/v1/projects", tags=["local runs"], route_class=BatchRoute, responses={401: {"model": ErrorBody}}
)


# Models


def _line(value: str | None) -> str | None:
    """``value`` as one trimmed line: 422 for a blank one or one with a control character."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("this needs some text")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("this is one line: no control characters")
    return value


class LocalRegister(BaseModel):
    machine: str = Field(min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the machine's name")
    cwd: str = Field(min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the session's working directory")
    claude_email: str = Field(
        "unknown",
        min_length=1,
        max_length=runs.MAX_LOCAL_FIELD,
        description="the Claude account the session runs as; 'unknown' when the machine could not read it",
    )
    claude_profile: str | None = Field(
        None, min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the claude-profiles profile, when known"
    )
    title: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_TITLE_CHARS,
        description="what the session is about; the machine and directory when left out",
    )

    _lines = field_validator("machine", "cwd", "claude_email", "claude_profile", "title")(_line)


class LocalState(BaseModel):
    state: Literal[runs.LOCAL_STATES] | None = Field(
        None,
        description="running: a turn is under way; waiting: the agent waits for a decision; idle: the turn ended and "
        "the person has the word; ended: the session is over",
    )
    plan_id: str | None = Field(
        None, pattern=plan_routes.PLAN_ID, description="the plan the session works on, as the hub holds it"
    )
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of that plan it works on"
    )
    title: str | None = Field(None, min_length=1, max_length=MAX_TITLE_CHARS)

    _lines = field_validator("title")(_line)

    @model_validator(mode="after")
    def _plan_first(self):
        if self.step_key is not None and self.plan_id is None:
            raise ValueError("a step belongs to a plan: give plan_id with step_key")
        return self


class LocalDecisionAsk(DecisionAsk):
    category: Literal[runs.LOCAL_DECISION_CATEGORIES] = Field(
        runs.LOCAL_DECISION_CATEGORY, description="a question of the person fits 'question'"
    )


# The run of the member's session


async def _own(conn: AsyncConnection, user: Principal, project: str, run_id: int, *, write: bool = True):
    """(access, row) of local run ``run_id`` of ``project`` that ``user`` owns, its row locked: state, event_seq,
    events_acked, project_id, name (the project's), plan_id, plan_revision, step_key, dispatched_by and kind. 404 for
    any other run, as for none; 403 without the writer role (``write``) or any grant."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    if write:
        check_dispatcher(access)
    r, p = tables.runs, tables.projects
    query = (
        select(
            r.c.state,
            r.c.event_seq,
            r.c.events_acked,
            r.c.project_id,
            p.c.name,
            r.c.plan_id,
            r.c.plan_revision,
            r.c.step_key,
            r.c.dispatched_by,
            r.c.kind,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id, r.c.project_id == access.project_id, r.c.kind == "local")
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.dispatched_by != user.user_id:
        raise HTTPException(404, f"project {project} has no local run {run_id} of yours")
    return access, row


async def writing_run(conn: AsyncConnection, access: ProjectAccess, user: Principal, run_id: int) -> int:
    """``run_id`` when it is a local run of the project that ``user`` owns, its row locked, for a plan write that names
    it; 404 for any other run (``plans``)."""
    r = tables.runs
    query = (
        select(r.c.dispatched_by)
        .where(r.c.id == run_id, r.c.project_id == access.project_id, r.c.kind == "local")
        .with_for_update()
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.dispatched_by != user.user_id:
        raise HTTPException(404, f"project {access.name} has no local run {run_id} of yours to record the write on")
    return run_id


async def note_progress(conn: AsyncConnection, run_id: int, plan_id: str, revision: int, step: str | None) -> None:
    """Record on local run ``run_id`` (locked by ``writing_run``) the plan, revision and step its session just wrote.
    The step stays when the plan is the one the run has and the write names none."""
    r = tables.runs
    held = (await conn.execute(select(r.c.plan_id, r.c.step_key).where(r.c.id == run_id))).one()
    key = step if step is not None else (held.step_key if held.plan_id == plan_id else None)
    values = {"plan_id": plan_id, "plan_revision": revision, "step_key": key, "last_activity_at": func.now()}
    await conn.execute(update(r).values(values).where(r.c.id == run_id))


def _touch(run_id: int):
    r = tables.runs
    return update(r).values(last_activity_at=func.now()).where(r.c.id == run_id)


async def _end(conn: AsyncConnection, run_id: int, old: str, actor: str, reason: str, decisions: str) -> None:
    """End local run ``run_id`` (in ``old``, its row locked): its open decisions become ``decisions``."""
    r, d = tables.runs, tables.decisions
    ended = {"state": "ended", "finished_at": func.now(), "waiting_since": None}
    await conn.execute(update(r).values(**ended).where(r.c.id == run_id))
    await conn.execute(update(d).values(state=decisions).where(d.c.run_id == run_id, d.c.state == "open"))
    await write_state(conn, run_id, old, "ended", actor, reason)


async def _register(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, session_id: str, body: LocalRegister
):
    """(run id, created) of the local run of ``session_id``: made now, or the member's own one updated."""
    r = tables.runs
    title = body.title or runs.clip(f"{body.machine}: {body.cwd}", MAX_TITLE_CHARS)
    fields = {
        "machine": body.machine,
        "cwd": body.cwd,
        "claude_email": body.claude_email,
        "claude_profile": body.claude_profile,
    }
    made = (
        pg_insert(r)
        .values(
            project_id=access.project_id,
            kind="local",
            title=title,
            dispatched_by=user.user_id,
            dispatched_via="web" if user.kind == WEB else "machine",
            requested_runtime="claude-code",
            runtime="claude-code",
            mode="interactive",
            approval="auto",
            timeout_s=86400,  # runs.timeout_s is at most a day; nothing times a local run out
            state="running",
            session_id=session_id,
            last_activity_at=func.now(),
            **fields,
        )
        .on_conflict_do_nothing(index_elements=[r.c.project_id, r.c.session_id], index_where=r.c.kind == "local")
        .returning(r.c.id)
    )
    created = (await conn.execute(made)).scalar_one_or_none()
    if created is not None:
        await write_state(conn, created, "none", "running", "session", f"{body.machine} registered the session")
        return created, True
    held = select(r.c.id, r.c.state, r.c.dispatched_by).where(
        r.c.project_id == access.project_id, r.c.session_id == session_id, r.c.kind == "local"
    )
    found = (await conn.execute(held.with_for_update())).one()
    if found.dispatched_by != user.user_id:
        raise HTTPException(403, f"session {session_id} is registered by another member of project {access.name}")
    values = {**fields, "last_activity_at": func.now()}
    if body.title is not None:
        values["title"] = body.title
    if found.state == "ended":  # the session is back
        values |= {"state": "running", "finished_at": None}
    await conn.execute(update(r).values(**values).where(r.c.id == found.id))
    if found.state == "ended":
        await write_state(conn, found.id, "ended", "running", "session", f"{body.machine} registered the session again")
    return found.id, False


@router.put(
    "/{project}/local-runs/{session_id}",
    response_model=Run,
    status_code=200,
    responses={201: {"model": Run, "description": "the session was not registered yet"}, **REFUSED},
)
async def register(
    request: Request,
    response: Response,
    project: ProjectName,
    session_id: SESSION_ID,
    body: LocalRegister,
    user: CurrentUser,
) -> Run:
    """Register the Claude Code session ``session_id`` of the caller's machine, or say again that it is there: the
    run of that session, new (201) or the caller's own (200), whose machine, directory, account and title are the ones
    given and which runs again when it had ended."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        check_dispatcher(access)
        run_id, created = await _register(conn, user, access, session_id, body)
        view = await run_view(conn, run_id)
    if created:
        response.status_code = 201
        log.info("local run registered", extra={"run_id": run_id, "login": user.login, "machine": body.machine})
    return view


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


@router.post(
    "/{project}/local-runs/{run_id}/state", response_model=Run, responses={**REFUSED, 409: {"model": ErrorBody}}
)
async def report_state(
    request: Request, project: ProjectName, run_id: RunId, body: LocalState, user: CurrentUser
) -> Run:
    """Report the state of the session, the plan and step it works on, and its title; every call is a sign of life.
    An ended run takes nothing but the PUT that registers the session again (409)."""
    r = tables.runs
    async with request.app.state.engine.begin() as conn:
        access, row = await _own(conn, user, project, run_id)
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
        new = body.state or row.state
        if new == "ended":
            await conn.execute(update(r).values(**values).where(r.c.id == run_id))
            await _end(conn, run_id, row.state, "session", "the session ended", "cancelled")
        else:
            values["state"] = new
            values["waiting_since"] = func.now() if new == "waiting" and row.state != "waiting" else None
            if new == "waiting" and row.state == "waiting":
                values.pop("waiting_since")
            await conn.execute(update(r).values(**values).where(r.c.id == run_id))
            if new != row.state:
                await write_state(conn, run_id, row.state, new, "session", f"the session is {new}")
        view = await run_view(conn, run_id)
    return view


@batch_router.post(
    "/{project}/local-runs/{run_id}/events",
    response_model=EventsAck,
    responses={**REFUSED, 413: {"model": ErrorBody}},
)
async def post_events(
    request: Request, project: ProjectName, run_id: RunId, body: EventBatch, user: CurrentUser
) -> EventsAck:
    """Store the events of the batch that follow the ones stored, and say up to where the machine's spool may go: as
    the worker's POST /v1/worker/runs/{id}/events does."""
    async with request.app.state.engine.begin() as conn:
        _, row = await _own(conn, user, project, run_id)
        await conn.execute(_touch(run_id))
        return await store_events(request, conn, run_id, row.state, row.event_seq, row.events_acked, body.events)


# Decisions


@router.post(
    "/{project}/local-runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSED, 409: {"model": ErrorBody}},
)
async def ask(request: Request, project: ProjectName, run_id: RunId, body: LocalDecisionAsk, user: CurrentUser):
    """Ask the member a decision and notify them: the run waits while one is open. 409 for a run that ended and past
    the open decisions a run may hold."""
    r = tables.runs
    async with request.app.state.engine.begin() as conn:
        _, row = await _own(conn, user, project, run_id)
        if row.state == "ended":
            raise HTTPException(409, f"local run {run_id} ended: register the session again to ask a decision")
        if row.state != "waiting":
            waiting = {"state": "waiting", "waiting_since": func.now()}
            await conn.execute(update(r).values(**waiting, last_activity_at=func.now()).where(r.c.id == run_id))
            await write_state(conn, run_id, row.state, "waiting", "session", "the session asks a decision")
        else:
            await conn.execute(_touch(run_id))
        view = await store_decision(conn, run_id, row, body)
    log.info("local decision asked", extra={"run_id": run_id, "decision_id": view.id, "category": body.category})
    return view


async def _decision_of(conn: AsyncConnection, run_id: int, decision_id: int, project: str):
    d = tables.decisions
    found = select(d.c.state, d.c.delivered_at).where(d.c.id == decision_id, d.c.run_id == run_id)
    row = (await conn.execute(found)).one_or_none()
    if row is None:
        raise HTTPException(404, f"local run {run_id} of project {project} has no decision {decision_id}")
    return row


async def _taken(conn: AsyncConnection, run_id: int, decision_id: int) -> None:
    """The machine has the answer of decision ``decision_id``: it and its inbox message are delivered."""
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(
        update(d)
        .values(delivered_at=func.now())
        .where(d.c.id == decision_id, d.c.state == "answered", d.c.delivered_at.is_(None))
    )
    inbox = update(i).values(delivered_at=func.now())
    await conn.execute(inbox.where(i.c.run_id == run_id, i.c.decision_id == decision_id, i.c.delivered_at.is_(None)))


@router.get(
    "/{project}/local-runs/{run_id}/decisions/{decision_id}",
    response_model=Decision,
    responses={**REFUSED},
)
async def take_decision(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    decision_id: DecisionId,
    user: CurrentUser,
    wait_s: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description="hold the request up to this many seconds while the decision is open; 0 answers at once",
        ),
    ] = 0,
) -> Decision:
    """The decision as it is now: once it is answered, the answer is in it (``answer_option``, ``answer_text``), and
    the machine has taken it. With ``wait_s`` the request waits for the answer (long-poll) and holds no connection
    of the database meanwhile; ask again when it comes back open."""
    streams: RunStreams = request.app.state.run_streams
    streams.start()
    deadline = time.monotonic() + wait_s
    with streams.subscribe(run_id) as wakeup:
        while True:
            async with request.app.state.engine.begin() as conn:
                await _own(conn, user, project, run_id, write=False)
                state = (await _decision_of(conn, run_id, decision_id, project)).state
                left = deadline - time.monotonic()
                if state != "open" or left <= 0:
                    if state == "answered":
                        await _taken(conn, run_id, decision_id)
                    return await decision_view(conn, decision_id)
            await wakeup.wait(min(POLL_SECONDS, left))


@router.post(
    "/{project}/local-runs/{run_id}/decisions/{decision_id}/answered",
    response_model=Decision,
    responses={**REFUSED, 409: {"model": ErrorBody}},
)
async def answered_on_the_spot(
    request: Request, project: ProjectName, run_id: RunId, decision_id: DecisionId, body: AnswerIn, user: CurrentUser
) -> Decision:
    """Report that the person answered decision ``decision_id`` in the session itself: an answer like any other, so
    the first of the web, Telegram, ``hub decision answer`` and this wins, and a later one gets 409 with who got
    there first. Nothing goes to the session's inbox: it has the answer."""
    r, d, u = tables.runs, tables.decisions, tables.users
    async with request.app.state.engine.begin() as conn:
        access, row = await _own(conn, user, project, run_id)
        locked = (
            select(d.c.state, d.c.options, d.c.step_key, d.c.answered_by, u.c.login)
            .join_from(d, u, u.c.id == d.c.answered_by, isouter=True)
            .where(d.c.id == decision_id, d.c.run_id == run_id)
            .with_for_update(of=d)
        )
        found = (await conn.execute(locked)).one_or_none()
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
        answered = {
            "state": "answered",
            "answer_option": body.option,
            "answer_text": body.text,
            "answered_by": user.user_id,
            "answered_at": func.now(),
            "delivered_at": func.now(),
        }
        await conn.execute(update(d).values(**answered).where(d.c.id == decision_id))
        n = tables.notifications
        read = update(n).values(read_at=func.now())
        await conn.execute(
            read.where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
        )
        still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
        if row.state == "waiting" and (await conn.execute(still_open)).scalar_one() == 0:
            await conn.execute(update(r).values(state="running", waiting_since=None).where(r.c.id == run_id))
            await write_state(conn, run_id, "waiting", "running", "session", "the person answered on the spot")
        await conn.execute(_touch(run_id))
        await write_event(conn, run_id, {"text": f"decision #{decision_id} answered on the machine"})
        target = _answer_target(project, row.plan_id, found.step_key, decision_id, run_id, body.option, "local")
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


@router.post("/{project}/local-runs/{run_id}/inbox", response_model=Inbox, responses=REFUSED)
async def take_inbox(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, body: InboxAck | None = None
) -> Inbox:
    """Mark the messages up to ``ack`` delivered, and hand out the ones that still wait: the answers to the run's
    decisions, as a worker's inbox hands them to its agent."""
    ack = (body or InboxAck()).ack
    async with request.app.state.engine.begin() as conn:
        await _own(conn, user, project, run_id)
        await conn.execute(_touch(run_id))
        if ack is not None:
            await conn.execute(_deliver(run_id, ack))
        rows = (await conn.execute(_undelivered(run_id))).all()
    return Inbox(messages=[InboxMessage(**row._mapping) for row in rows])


# The reaper


async def end_silent(engine, batch: int = 500, silent_for: int = runs.LOCAL_SILENT_SECONDS) -> int:
    """End the local runs that sent nothing for ``silent_for`` seconds, at most ``batch`` of them, each in a
    transaction of its own that skips a run another holds; their open decisions expire. How many it ended."""
    r = tables.runs
    cutoff = func.now() - timedelta(seconds=silent_for)
    silent = (r.c.kind == "local", r.c.state != "ended", r.c.last_activity_at < cutoff)
    async with engine.begin() as conn:
        due = [row.id for row in await conn.execute(select(r.c.id).where(*silent).order_by(r.c.id).limit(batch))]
    ended = 0
    for run_id in due:
        async with engine.begin() as conn:
            locked = select(r.c.state).where(r.c.id == run_id, *silent).with_for_update(skip_locked=True)
            found = (await conn.execute(locked)).one_or_none()
            if found is None:  # it spoke meanwhile, or another reaper has it
                continue
            reason = f"nothing from the session for {silent_for // 3600} h"
            await _end(conn, run_id, found.state, "hub", reason, "expired")
            ended += 1
    if ended:
        log.info("silent local runs ended", extra={"count": ended})
    return ended


__all__ = ["end_silent", "note_progress", "router", "batch_router", "writing_run"]
