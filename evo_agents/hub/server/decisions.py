"""Decisions: the questions a plan run's agent asks its owner, and the owner's answers. ``docs/notifications.md``
(Decisions) is the design, ``runs`` holds the categories, states and limits, and ``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run (404 for any other run, a run
of one step included): a category of ``runs.DECISION_CATEGORIES`` (422 for any other), a question, a context in
markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the agent recommends, and the step
it is about, which must be a step of the plan. It answers 201 with the decision, whose id the agent waits for, leaves
a ``system`` event in the run's log and notifies the run's owner (``notifications.notify``). A run has at most
MAX_OPEN_DECISIONS open at once (409).

POST /v1/projects/{p}/decisions/{id}/answer is for the run's owner alone, with a web session or a machine token of
their own: another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/), and someone
without a grant on the project 404. A machine token also gets 403 when the run is on a worker set to take runs
dispatched from the web only, held, pinned or parked there (``runs.web_only_steering``): its answer would steer that
worker's agent, or resume the parked run on it. A decision that is no longer open gets 409. The answer names an
option of the decision, gives text of the owner's own (at most 4 KiB), or both. Under the plan's dispatch lock and the
run's row lock, the decision is answered, its notification read, and the answer goes to a run's inbox as a message
that names the decision (``run_inbox.decision_id``), with a ``user_message`` event, so the heartbeat counts it and the
worker hands it to the agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox.

The audit row decision.answer names the decision, its run and the option, never the text.

GET /v1/projects/{p}/decisions lists a project's decisions, newest first, filtered by state, run and plan, a page at a
time, and GET .../decisions/{id} shows one, to the readers of the run's plan (``runs.visible_plans``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import BigInteger, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import Select

from evo_agents.hub import runs, tables
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import decision_link, notify, one_line
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs import (
    LINE,
    MAX_ID,
    REFUSALS,
    RunId,
    _dispatcher,
    _held_plan_run,
    _lock_plan,
    visible_plans,
    web_only_steering,
)
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_OPEN_DECISIONS = 20  # a run's agent asks at most this many before one is answered
LABEL_CHARS = 200  # an option's label, one line
DESCRIPTION_CHARS = 1000  # an option's description
MAX_OPTIONS_BYTES = 16 * 1024  # all the options of a decision as JSON, as schema 0010 bounds them
QUESTION_IN_MESSAGE = 500  # characters of the question the answer's inbox message repeats
MAX_LIST = 200
MAX_OFFSET = 100_000

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})

DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]
OptionKey = Annotated[str, Field(pattern=runs.OPTION_KEY, description="letters, digits, _ and -, at most 32")]


# Models


class OptionIn(BaseModel):
    key: OptionKey
    label: str = Field(min_length=1, max_length=LABEL_CHARS, pattern=LINE, description="one line")
    description: str | None = Field(None, min_length=1, max_length=DESCRIPTION_CHARS)


class DecisionAsk(BaseModel):
    category: Literal[runs.DECISION_CATEGORIES] = Field(description="the agent asks only about these")
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(
        None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES, description="markdown, at most 16 KiB of UTF-8"
    )
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    recommended: OptionKey | None = Field(None, description="the key of the option the agent recommends")
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )

    @model_validator(mode="after")
    def _consistent(self):
        if not self.question.strip():
            raise ValueError("a decision needs a question")
        if self.context is not None and len(self.context.encode()) > runs.MAX_DECISION_CONTEXT_BYTES:
            raise ValueError(f"a decision's context is at most {runs.MAX_DECISION_CONTEXT_BYTES} bytes of UTF-8")
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("each option of a decision needs a key of its own")
        if self.recommended is not None and self.recommended not in keys:
            raise ValueError(f"the recommended option {self.recommended!r} is not one of {', '.join(keys)}")
        if len(json.dumps(self.stored_options(), ensure_ascii=False).encode()) > MAX_OPTIONS_BYTES:
            raise ValueError(f"the options of a decision take at most {MAX_OPTIONS_BYTES} bytes of JSON")
        return self

    def stored_options(self) -> list[dict]:
        stored = []
        for option in self.options:
            entry = {"key": option.key, "label": option.label}
            if option.description is not None:
                entry["description"] = option.description
            if option.key == self.recommended:
                entry["recommended"] = True
            stored.append(entry)
        return stored


class DecisionOption(BaseModel):
    key: str
    label: str
    description: str | None
    recommended: bool


class Decision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the plan run the decision belongs to")
    run_state: Literal[runs.RUN_STATES]
    plan_id: str
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.DECISION_CATEGORIES]
    question: str
    context: str | None = Field(description="markdown")
    options: list[DecisionOption]
    recommended: str | None = Field(description="the key of the option the agent recommends")
    state: Literal[runs.DECISION_STATES]
    owner: str = Field(description="the login of the run's owner, the one member who may answer")
    answer_option: str | None
    answer_text: str | None
    answered_by: str | None
    answer_run_id: int | None = Field(
        description="the run whose inbox the answer went to: the decision's run, or the run that resumed it"
    )
    asked_at: datetime
    answered_at: datetime | None
    delivered_at: datetime | None = Field(description="when the worker handed the answer to the agent")


class DecisionList(BaseModel):
    decisions: list[Decision] = Field(description="newest first")
    total: int = Field(description="decisions that match the filters")
    limit: int
    offset: int


class AnswerIn(BaseModel):
    option: OptionKey | None = Field(None, description="the key of one of the decision's options")
    text: str | None = Field(
        None, min_length=1, max_length=runs.MAX_ANSWER_BYTES, description="the owner's own words, at most 4 KiB"
    )

    @model_validator(mode="after")
    def _something(self):
        if self.text is not None and not self.text.strip():
            raise ValueError("an answer's text needs some words")
        if self.text is not None and len(self.text.encode()) > runs.MAX_ANSWER_BYTES:
            raise ValueError(f"an answer's text is at most {runs.MAX_ANSWER_BYTES} bytes of UTF-8")
        if self.option is None and self.text is None:
            raise ValueError("an answer names an option, gives text, or both")
        return self


# Reading decisions


def _decisions():
    """The columns of Decision but its options as stored, named as its fields: the decision with its project, its
    run's state and owner, who answered, and the run whose inbox the answer went to (the first message naming it)."""
    d, p, r, i = tables.decisions, tables.projects, tables.runs, tables.run_inbox
    owner, answerer = tables.users.alias("o"), tables.users.alias("a")
    answer_run = select(i.c.run_id).where(i.c.decision_id == d.c.id).order_by(i.c.id).limit(1).scalar_subquery()
    return select(
        d.c.id,
        p.c.name.label("project"),
        d.c.run_id,
        r.c.state.label("run_state"),
        d.c.plan_id,
        d.c.step_key,
        d.c.category,
        d.c.question,
        d.c.context,
        d.c.options,
        d.c.state,
        owner.c.login.label("owner"),
        d.c.answer_option,
        d.c.answer_text,
        answerer.c.login.label("answered_by"),
        answer_run.label("answer_run_id"),
        d.c.asked_at,
        d.c.answered_at,
        d.c.delivered_at,
    ).select_from(
        d.join(p, p.c.id == d.c.project_id)
        .join(r, r.c.id == d.c.run_id)
        .join(owner, owner.c.id == r.c.dispatched_by)
        .outerjoin(answerer, answerer.c.id == d.c.answered_by)
    )


def _listed(project_id: int, plans: list[str], states: list[str], run_id: int | None, plan_id: str | None) -> list:
    """The filters of the decision list: the project's decisions of ``plans``, in any of ``states`` (any state when
    empty), of run ``run_id`` and plan ``plan_id`` when given."""
    d = tables.decisions
    where = [d.c.project_id == project_id, d.c.plan_id.in_(plans)]
    if states:
        where.append(d.c.state.in_(states))
    if run_id is not None:
        where.append(d.c.run_id == run_id)
    if plan_id is not None:
        where.append(d.c.plan_id == plan_id)
    return where


def _decision(row) -> Decision:
    fields = dict(row._mapping)
    shown = [
        DecisionOption(
            key=option["key"],
            label=option["label"],
            description=option.get("description"),
            recommended=option.get("recommended") is True,
        )
        for option in fields.pop("options")
    ]
    recommended = next((option.key for option in shown if option.recommended), None)
    return Decision(**fields, options=shown, recommended=recommended)


async def decision_view(conn: AsyncConnection, decision_id: int) -> Decision:
    row = (await conn.execute(_decisions().where(tables.decisions.c.id == decision_id))).one()
    return _decision(row)


def _no_decision(project: str, decision_id: int) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no decision {decision_id}: see GET /v1/projects/{project}/decisions"
    )


async def readable_decision(
    conn: AsyncConnection, user: Principal, project: str, decision_id: int, sink: str | None
) -> ProjectAccess:
    """The caller's access to ``project`` when it may read decision ``decision_id`` of it: a grant on the project (404
    without, 403 for a hub admin without one) and the decision's plan visible to it (404 otherwise, as for none)."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    d = tables.decisions
    query = select(d.c.plan_id).where(d.c.id == decision_id, d.c.project_id == access.project_id)
    plan_id = (await conn.execute(query)).scalar_one_or_none()
    if plan_id is None or plan_id not in await visible_plans(conn, access, sink):
        raise _no_decision(project, decision_id)
    return access


@router.get(
    "/{project}/decisions",
    response_model=DecisionList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_decisions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[Literal[runs.DECISION_STATES]], Query(description="any of these states; repeat it")] = [],  # noqa: B006
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    plan_id: Annotated[str | None, Query(pattern=plan_routes.PLAN_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> DecisionList:
    """The project's decisions, newest first, of the plans the caller may read."""
    d = tables.decisions
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        where = _listed(access.project_id, plans, list(dict.fromkeys(state)), run_id, plan_id)
        query = _decisions().where(*where).order_by(d.c.id.desc()).limit(limit).offset(offset)
        page = [_decision(row) for row in (await conn.execute(query)).all()]
        total = (await conn.execute(select(func.count()).select_from(d).where(*where))).scalar_one()
    return DecisionList(decisions=page, total=total, limit=limit, offset=offset)


@router.get(
    "/{project}/decisions/{decision_id}",
    response_model=Decision,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_decision(
    request: Request,
    project: ProjectName,
    decision_id: DecisionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Decision:
    async with request.app.state.engine.begin() as conn:
        await readable_decision(conn, user, project, decision_id, sink)
        return await decision_view(conn, decision_id)


# Asking


def _asked_step(run_id: int, plan, key: str | None) -> str | None:
    """The key the plan gives the step ``key`` names (an id or an order); 422 when the plan has no such step."""
    if key is None:
        return None
    try:
        index = step_index(plan, key)
    except (PlanProblem, TypeError) as exc:
        raise HTTPException(422, f"run {run_id}: {exc}; a decision is about a step of the run's plan") from None
    return step_key(plan["steps"][index], index)


@worker_router.post(
    "/runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: DecisionAsk, user: CurrentUser) -> Decision:
    """Ask the owner of a plan run this worker holds a decision, and notify them."""
    async with request.app.state.engine.begin() as conn:
        _, row = await _held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, _ = row
        d, pl = tables.decisions, tables.plans
        opened = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
        if (await conn.execute(opened)).scalar_one() >= MAX_OPEN_DECISIONS:
            raise HTTPException(
                409,
                f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
            )
        held = select(pl.c.body).where(pl.c.project_id == project_id, pl.c.plan_id == plan_id)
        current = (await conn.execute(held)).one_or_none()
        key = _asked_step(run_id, current.body if current else {}, body.step_key)
        stored = (
            insert(d)
            .values(
                run_id=run_id,
                project_id=project_id,
                plan_id=plan_id,
                step_key=key,
                category=body.category,
                question=body.question,
                context=body.context,
                options=body.stored_options(),
            )
            .returning(d.c.id)
        )
        decision_id = (await conn.execute(stored)).scalar_one()
        about = f"plan {plan_id}" + (f", step {key}" if key else "")
        await notify(
            conn,
            user_id=dispatcher_id,
            kind="decision",
            project_id=project_id,
            run_id=run_id,
            decision_id=decision_id,
            title=f"Run #{run_id} asks: {one_line(body.question)}",
            body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {project}.",
            link=decision_link(decision_id),
        )
        shown = f"decision #{decision_id} asked ({body.category}): {one_line(body.question)}"
        await write_event(
            conn, run_id, {"text": shown, "decision": {"id": decision_id, "category": body.category, "step": key}}
        )
        view = await decision_view(conn, decision_id)
    log.info("decision asked", extra={"run_id": run_id, "decision_id": decision_id, "category": body.category})
    return view


# Answering


class _OverridingSystemValue(Select):
    """The SELECT of an INSERT ... SELECT that gives a value for runs.id, which is GENERATED ALWAYS AS IDENTITY:
    Postgres takes one only after OVERRIDING SYSTEM VALUE, which SQLAlchemy has no construct for, so this select
    renders it in front of itself. Only ``_resume`` needs it: it reserves the new run's id with nextval first, so the
    parked run can end done "resumed as #N" before runs_active_plan_key lets the new plan run of its plan in."""

    inherit_cache = True


@compiles(_OverridingSystemValue)
def _overriding_system_value(element, compiler, **kw):
    return "OVERRIDING SYSTEM VALUE " + compiler.visit_select(element, **kw)


def _resume_run(new_id: int, parked_id: int):
    """The run that resumes parked plan run ``parked_id``, with id ``new_id``: pinned to its worker, in its session,
    with the agent time it used and the credential it was dispatched with, at the plan's current revision (the one it
    was dispatched from when the plan is gone); a run the night shift queued keeps its schedule, night and caps."""
    r, pl = tables.runs, tables.plans
    source = _OverridingSystemValue(
        literal(new_id, BigInteger).label("id"),
        literal("plan").label("kind"),
        r.c.project_id,
        r.c.plan_id,
        r.c.title,
        func.coalesce(pl.c.revision, r.c.plan_revision).label("plan_revision"),
        r.c.dispatched_by,
        r.c.dispatched_via,
        r.c.worker_id.label("pinned_worker_id"),
        r.c.runtime.label("requested_runtime"),
        r.c.runtime,
        r.c.model,
        r.c.mode,
        r.c.approval,
        r.c.timeout_s,
        r.c.max_attempts,
        r.c.repos,
        r.c.id.label("resume_of_run_id"),
        r.c.run_seconds,
        r.c.session_id,
        r.c.schedule_id,
        r.c.schedule_night,
        r.c.budget,
    )
    source = source.select_from(
        r.outerjoin(pl, (pl.c.project_id == r.c.project_id) & (pl.c.plan_id == r.c.plan_id))
    ).where(r.c.id == parked_id)
    return insert(r).from_select([column.name for column in source.selected_columns], source)


def answer_message(decision_id: int, category: str, question: str, option: dict | None, text: str | None) -> str:
    """The inbox message that hands the owner's answer to the agent: the decision it answers, the option chosen and
    the owner's own words, within MAX_MESSAGE_BYTES."""
    lines = [f"Answer to decision #{decision_id} ({category}): {one_line(question, QUESTION_IN_MESSAGE)}"]
    if option is not None:
        lines.append(f"Chosen option: {option['key']}, {option['label']}.")
    if text is not None:
        lines.append(text.strip() if option is not None else f"The owner's answer:\n{text.strip()}")
    return runs.clip("\n".join(lines), runs.MAX_MESSAGE_BYTES)


def _answer_target(project: str, plan_id: str, key: str | None, decision_id: int, run_id: int, option) -> str:
    step = "" if key is None else f"#{key}"
    return f"{project}/{plan_id}{step} decision:{decision_id} run:{run_id} option={option or '-'}"


async def _resume(conn: AsyncConnection, user: Principal, parked_id: int, decision_id: int) -> int:
    """Queue the run that resumes parked run ``parked_id``, in the caller's transaction under the plan's lock, and end
    the parked one done; the new run's id. The parked run's decisions still open go to the new run."""
    reserved = select(func.nextval(func.pg_get_serial_sequence("runs", "id")))
    new_id = (await conn.execute(reserved)).scalar_one()
    # The parked run leaves the active states first: a plan has one active plan run at a time.
    await move_run(
        conn,
        parked_id,
        "parked",
        "done",
        "owner",
        reason=f"resumed as #{new_id}",
        token_id=user.token_id,
        decisions=None,
    )
    await conn.execute(_resume_run(new_id, parked_id))
    d = tables.decisions
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    shown = f"resumes run #{parked_id} in its session, as {user.login} answered decision #{decision_id}"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": parked_id})
    await notify_queued(conn, new_id)
    return new_id


@dataclass(frozen=True)
class Answered:
    """A decision just answered: as the routes show it, its run, and the run whose inbox took the answer."""

    decision: Decision
    run_id: int
    inbox_run: int


async def answer_decision(
    conn: AsyncConnection, user: Principal, project: str, decision_id: int, body: AnswerIn, *, via: str | None = None
) -> Answered:
    """Answer decision ``decision_id`` of ``project`` as ``user``, in the caller's transaction: the checks, the answer,
    its notification read, the inbox message, a parked run resumed and the audit row, as the module's docstring says.
    The answer route and the Telegram channel (``via="telegram"``, which the audit row names) both answer through it;
    it raises the HTTPException the route answers with."""
    access = await readable_decision(conn, user, project, decision_id, None)
    d, r, u = tables.decisions, tables.runs, tables.users
    query = (
        select(d.c.plan_id, d.c.step_key, r.c.dispatched_by, u.c.login)
        .select_from(d.join(r, r.c.id == d.c.run_id).join(u, u.c.id == r.c.dispatched_by))
        .where(d.c.id == decision_id, d.c.project_id == access.project_id)
    )
    found = (await conn.execute(query)).one()
    plan_id, key, owner_id, owner = found.plan_id, found.step_key, found.dispatched_by, found.login
    if owner_id != user.user_id:
        raise HTTPException(403, f"only {owner}, who dispatched its run, may answer decision {decision_id}")
    _dispatcher(access)
    # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
    # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
    await _lock_plan(conn, access.project_id, plan_id)
    run_id = (await conn.execute(select(d.c.run_id).where(d.c.id == decision_id))).scalar_one()
    run_state = (await conn.execute(select(r.c.state).where(r.c.id == run_id).with_for_update())).scalar_one()
    locked = select(d.c.state, d.c.category, d.c.question, d.c.options).where(d.c.id == decision_id)
    state, category, question, options = (await conn.execute(locked.with_for_update())).one()
    if state != "open":
        raise HTTPException(409, f"decision {decision_id} is {state}, not open: it takes no answer any more")
    chosen = None
    if body.option is not None:
        chosen = next((option for option in options if option["key"] == body.option), None)
        if chosen is None:
            keys = ", ".join(option["key"] for option in options)
            raise HTTPException(422, f"decision {decision_id} has no option {body.option!r}; its options are {keys}")
    if run_state != "parked" and run_state not in runs.MESSAGE_STATES:
        raise HTTPException(409, f"run {run_id} of decision {decision_id} is {run_state}: no agent can take an answer")
    await web_only_steering(conn, user, run_id, "answer its decisions", "answer on the web", "answered")
    await conn.execute(
        update(d)
        .values(
            state="answered",
            answer_option=body.option,
            answer_text=body.text,
            answered_by=user.user_id,
            answered_at=func.now(),
        )
        .where(d.c.id == decision_id)
    )
    n = tables.notifications
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    inbox_run = await _resume(conn, user, run_id, decision_id) if run_state == "parked" else run_id
    text = answer_message(decision_id, category, question, chosen, body.text)
    i = tables.run_inbox
    left = (
        insert(i).values(run_id=inbox_run, sent_by=user.user_id, body=text, decision_id=decision_id).returning(i.c.id)
    )
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": user.login, "message_id": message_id, "decision_id": decision_id}
    await write_user_message(conn, inbox_run, event)
    target = _answer_target(project, plan_id, key, decision_id, run_id, body.option)
    if inbox_run != run_id:
        target += f" resumed as run:{inbox_run}"
    if via is not None:
        target += f" via={via}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target,
        project_id=access.project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, inbox_run)


@router.post(
    "/{project}/decisions/{decision_id}/answer",
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def answer(
    request: Request, project: ProjectName, decision_id: DecisionId, body: AnswerIn, user: CurrentUser
) -> Decision:
    """Answer a decision of a run one dispatched: the answer goes to the agent through the run's inbox, and a parked
    run is resumed on its worker in its session."""
    async with request.app.state.engine.begin() as conn:
        answered = await answer_decision(conn, user, project, decision_id, body)
    log.info(
        "decision answered",
        extra={
            "decision_id": decision_id,
            "run_id": answered.run_id,
            "inbox_run": answered.inbox_run,
            "login": user.login,
        },
    )
    return answered.decision
