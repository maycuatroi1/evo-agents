"""Decisions: the questions the agent of a plan run or an author run asks its owner, and the owner's answers.
``docs/notifications.md`` (Decisions) is the design, ``runs`` holds the categories, states and limits, and
``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run, an author run or a chat run
(404 for any other run: a run of one step, a review run, a judge run): a category of ``runs.DECISION_CATEGORIES`` (a
chat run's is ``triage`` alone, and ``triage`` is no other run's; 422 for any other), a question, a context in markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the
agent recommends, the step it is about, which must be a step of the run's plan (422 for an author run that has put
no plan yet, which has no steps), and the outputs of the run it carries (``output_ids``, 422 for one of another run;
``output_store``), which every view of the decision lists with the decision's URL of their bytes. The decision keeps the plan the run has when its agent asks, none (``plan_id`` null)
for an author run that has put no plan yet. It answers 201 with the decision, whose id the agent waits for, leaves a
``system`` event in the run's log and notifies the run's owner (``notifications.notify``); the notification of a
decision without a plan names the author run and its title instead. A run has at most MAX_OPEN_DECISIONS open at once
(409).

POST /v1/projects/{p}/decisions/{id}/answer is for the run's owner alone, with a web session or a machine token of
their own: another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/), and someone
without a grant on the project 404. A machine token also gets 403 when the run is on a worker set to take runs
dispatched from the web only, held, pinned or parked there (``runs.web_only_steering``): its answer would steer that
worker's agent, or resume the parked run on it. A decision that is no longer open gets 409. The answer names an
option of the decision, gives text of the owner's own (at most 4 KiB), or both. Under the plan's dispatch lock (for a
decision with a plan) and the run's row lock, the decision is answered, its notification read, and the answer goes to
a run's inbox as a message that names the decision (``run_inbox.decision_id``), with a ``user_message`` event, so the
heartbeat counts it and the worker hands it to the agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked plan run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox;
- a parked author run is resumed so only once the answer leaves none of its decisions open. Until then the answer waits
  in the parked run's inbox, and the run that resumes it (on the last answer, or on a reply in its chat) takes every
  message of that inbox the worker has not handed to an agent, so the agent reads them all in its first turn.

The audit row decision.answer names the decision, its run and the option, never the text.

A decision of category release is the hub's own, asked of the owner of a release cycle that stopped, on the cycle's
last run (``release_cycles``): its answer names one of its options, which ``release_answer`` does (go on, close the
cycle, or pause the schedule) whatever the state of that run, and goes to no inbox. So is a decision of category loop,
asked of the person a work item of the Loop stands for once it failed a third time, on its last run (``loop.stall``):
``stall.answer_loop`` does the option chosen (run it again, drop it, leave it to a person).

GET /v1/projects/{p}/decisions lists a project's decisions, newest first, filtered by state, run and plan, a page at a
time, and GET .../decisions/{id} shows one, to the readers of the decision's run, as ``views.readable_run`` reads it:
the readers of its plan (``views.visible_plans``), and for an author run that has put no plan, those who read the
project's runs on no plan (``views.sees_unplanned``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import BigInteger, and_, false, func, insert, literal, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import Select

from evo_agents.hub import outputs, runs, tables
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import decision_link, notify, one_line
from evo_agents.hub.server.output_store import link_decision
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.output_views import by_decision as outputs_by_decision
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.models import LINE, MAX_ID, REFUSALS, RunId
from evo_agents.hub.server.runs.service.controls import web_only_steering
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.views import lock_plan, sees_unplanned, visible_plans
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
    category: Literal[runs.ASKED_DECISION_CATEGORIES] = Field(description="the agent asks only about these")
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(
        None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES, description="markdown, at most 16 KiB of UTF-8"
    )
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    recommended: OptionKey | None = Field(None, description="the key of the option the agent recommends")
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )
    output_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list,
        max_length=outputs.MAX_ATTACHED,
        description="outputs of the run the question carries, such as the screenshot it is about",
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
    run_id: int = Field(description="the plan run or author run the decision belongs to")
    run_state: Literal[runs.SHOWN_STATES]
    plan_id: str | None = Field(
        description="the run's plan when its agent asked; null for an author run that had put no plan yet"
    )
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.SHOWN_DECISION_CATEGORIES]
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
    outputs: list[RunOutput] = Field(
        default_factory=list,
        description="the files of its run it carries, oldest first, each with the decision's URL of its bytes",
    )


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


def _readable(plans: list[str], unplanned: bool):
    """Whether the caller reads a decision, of a query that joins its run: the run's plan is one of ``plans``, or the
    run has none and ``unplanned`` (``views.sees_unplanned``), as ``views.readable_run`` reads the run."""
    d, r = tables.decisions, tables.runs
    plan = func.coalesce(d.c.plan_id, r.c.plan_id)
    return or_(plan.in_(plans), and_(plan.is_(None), true() if unplanned else false()))


def _listed(
    project_id: int,
    plans: list[str],
    unplanned: bool,
    states: list[str],
    run_id: int | None,
    plan_id: str | None,
    viewer: int,
) -> list:
    """The filters of the decision list, of a query that joins each decision's run: the project's decisions the
    caller reads (``_readable``), in any of ``states`` (any state when empty), of run ``run_id`` and plan ``plan_id``
    when given; a local run's decisions are its owner's alone (``viewer`` is the caller)."""
    d, r = tables.decisions, tables.runs
    where = [
        d.c.project_id == project_id,
        _readable(plans, unplanned),
        or_(r.c.kind != "local", r.c.dispatched_by == viewer),
    ]
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
    (view,) = await with_outputs(conn, [_decision(row)])
    return view


async def with_outputs(conn: AsyncConnection, decisions: list[Decision]) -> list[Decision]:
    """``decisions``, each with the outputs it carries."""
    by_project: dict[str, list[int]] = {}
    for decision in decisions:
        by_project.setdefault(decision.project, []).append(decision.id)
    for project, ids in by_project.items():
        found = await outputs_by_decision(conn, project, ids)
        for decision in decisions:
            if decision.project == project:
                decision.outputs = found.get(decision.id, [])
    return decisions


def _no_decision(project: str, decision_id: int) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no decision {decision_id}: see GET /v1/projects/{project}/decisions"
    )


async def readable_decision(
    conn: AsyncConnection, user: Principal, project: str, decision_id: int, sink: str | None
) -> ProjectAccess:
    """The caller's access to ``project`` when it may read decision ``decision_id`` of it: a grant on the project (404
    without, 403 for a hub admin without one) and the decision's run readable to it (404 otherwise, as for none): its
    plan visible, or for an author run that has put no plan, the project's runs on no plan (``views.readable_run``)."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    d, r = tables.decisions, tables.runs
    query = (
        select(func.coalesce(d.c.plan_id, r.c.plan_id), r.c.kind, r.c.dispatched_by)
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(d.c.id == decision_id, d.c.project_id == access.project_id)
    )
    found = (await conn.execute(query)).one_or_none()
    if found is None or (found.kind == "local" and found.dispatched_by != user.user_id):  # a local run is its owner's
        raise _no_decision(project, decision_id)
    if found[0] is None:
        visible = sees_unplanned(access, sink)
    else:
        visible = found[0] in await visible_plans(conn, access, sink)
    if not visible:
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
    """The project's decisions, newest first, of the runs the caller may read."""
    d, r = tables.decisions, tables.runs
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        unplanned = sees_unplanned(access, sink)
        where = _listed(access.project_id, plans, unplanned, list(dict.fromkeys(state)), run_id, plan_id, user.user_id)
        query = _decisions().where(*where).order_by(d.c.id.desc()).limit(limit).offset(offset)
        page = await with_outputs(conn, [_decision(row) for row in (await conn.execute(query)).all()])
        counted = select(func.count()).select_from(d.join(r, r.c.id == d.c.run_id)).where(*where)
        total = (await conn.execute(counted)).scalar_one()
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


async def _step_of_run(conn: AsyncConnection, run_id: int, row, key: str | None) -> str | None:
    """The step of its plan a decision of run ``run_id`` (``row``, as ``held_plan_run`` reads it) is about; 422 for a
    step its plan does not have, and for any step of an author run that has put no plan yet."""
    if row.plan_id is None:
        if key is not None:
            raise HTTPException(
                422,
                f"{row.kind} run {run_id} has put no plan on the hub yet, so it has no step {key!r}: leave out "
                "step_key, or put the plan first",
            )
        return None
    pl = tables.plans
    held = select(pl.c.body).where(pl.c.project_id == row.project_id, pl.c.plan_id == row.plan_id)
    current = (await conn.execute(held)).one_or_none()
    return _asked_step(run_id, current.body if current else {}, key)


def about_run(run_id: int, kind: str, title: str | None) -> str:
    """What a decision of run ``run_id`` is about when it has no plan: the author run and its title."""
    shown = f"{kind} run #{run_id}"
    return shown + (f", {one_line(title)}" if title else "")


async def _notify_asked(conn: AsyncConnection, row, run_id: int, decision_id: int, key: str | None, body) -> None:
    """Tell the run's owner of the decision just asked: what it is about, its plan and step, or for an author run
    without a plan, the run and its title."""
    r = tables.runs
    title = (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one()
    if row.plan_id is not None:
        about = f"plan {row.plan_id}" + (f", step {key}" if key else "")
    else:
        about = about_run(run_id, row.kind, title)
    who = "Run" if row.kind == "plan" else f"{row.kind.capitalize()} run"
    await notify(
        conn,
        user_id=row.dispatched_by,
        kind="decision",
        project_id=row.project_id,
        run_id=run_id,
        decision_id=decision_id,
        title=f"{who} #{run_id} asks: {one_line(body.question)}",
        body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {row.name}.",
        link=decision_link(decision_id),
    )


async def store_decision(conn: AsyncConnection, run_id: int, row, body: DecisionAsk) -> Decision:
    """Store the decision ``body`` asks for run ``run_id``, whose row the caller holds locked, in the caller's
    transaction, with the outputs of the run it names, notify the run's owner and log it: ``row`` is what
    ``held_plan_run`` reads, or for a local run (``local_runs``) the same columns. 409 past MAX_OPEN_DECISIONS open,
    422 for a step the plan does not have or an output of another run."""
    d = tables.decisions
    opened = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    if (await conn.execute(opened)).scalar_one() >= MAX_OPEN_DECISIONS:
        raise HTTPException(
            409,
            f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
        )
    key = await _step_of_run(conn, run_id, row, body.step_key)
    stored = (
        insert(d)
        .values(
            run_id=run_id,
            project_id=row.project_id,
            plan_id=row.plan_id,
            step_key=key,
            category=body.category,
            question=body.question,
            context=body.context,
            options=body.stored_options(),
        )
        .returning(d.c.id)
    )
    decision_id = (await conn.execute(stored)).scalar_one()
    await link_decision(conn, run_id, decision_id, body.output_ids)
    await _notify_asked(conn, row, run_id, decision_id, key, body)
    shown = f"decision #{decision_id} asked ({body.category}): {one_line(body.question)}"
    await write_event(
        conn, run_id, {"text": shown, "decision": {"id": decision_id, "category": body.category, "step": key}}
    )
    return await decision_view(conn, decision_id)


def _in_its_category(run_id: int, kind: str, category: str) -> None:
    """422 unless ``category`` is one the agent of a run of ``kind`` asks in: a chat run asks only
    ``runs.CHAT_DECISION_CATEGORY``, the other kinds those of ``runs.DECISION_CATEGORIES``."""
    if (kind == "chat") != (category == runs.CHAT_DECISION_CATEGORY):
        if kind == "chat":
            allowed = f"only {runs.CHAT_DECISION_CATEGORY!r}"
        else:
            allowed = ", ".join(runs.DECISION_CATEGORIES)
        raise HTTPException(
            422, f"run {run_id} is a {kind} run: its agent asks decisions of {allowed}, not {category!r}"
        )


@worker_router.post(
    "/runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: DecisionAsk, user: CurrentUser) -> Decision:
    """Ask the owner of a plan run, an author run or a chat run this worker holds a decision, and notify them."""
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RESUME_KINDS)
        _in_its_category(run_id, row.kind, body.category)
        view = await store_decision(conn, run_id, row, body)
    log.info("decision asked", extra={"run_id": run_id, "decision_id": view.id, "category": body.category})
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
    """The run that resumes parked run ``parked_id`` (a plan run or an author run, ``runs.RESUME_KINDS``), with id
    ``new_id``, of its kind: pinned to its worker, in its session, with the agent time it used and the credential it
    was dispatched with, at the plan's current revision (the one it was dispatched from when the plan is gone; none for
    an author run that has put no plan); a run the night shift queued keeps its schedule, night and caps, an author
    run its request, a plan run its session per step, and every run its agent profile and version."""
    r, pl = tables.runs, tables.plans
    source = _OverridingSystemValue(
        literal(new_id, BigInteger).label("id"),
        r.c.kind,
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
        r.c.request,
        r.c.session_per_step,
        r.c.browser,
        r.c.profile_id,
        r.c.profile_version,
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


def answer_target(
    project: str, plan_id: str | None, key: str | None, decision_id: int, run_id: int, option, kind: str = "author"
) -> str:
    step = "" if key is None else f"#{key}"
    where = f"{project}/{plan_id}{step}" if plan_id is not None else f"{project}/{kind}"
    return f"{where} decision:{decision_id} run:{run_id} option={option or '-'}"


async def _resume(conn: AsyncConnection, user: Principal, parked_id: int, decision_id: int, text: str) -> int:
    """Queue the run that resumes parked run ``parked_id``, in the caller's transaction under the plan's lock, as
    ``user`` answered decision ``decision_id`` with the inbox message ``text``; the new run's id (``resume_parked``)."""
    why = f"as {user.login} answered decision #{decision_id}"
    return await resume_parked(conn, user, parked_id, why, woken_by=text)


async def resume_parked(
    conn: AsyncConnection, user: Principal, parked_id: int, why: str, *, woken_by: str | None = None
) -> int:
    """Queue the run that resumes parked run ``parked_id``, whose row the caller holds locked (for a plan run, under
    the plan's lock), and end the parked one done; the new run's id. ``why`` ends the new run's first event, such as
    "as owner answered decision #3". The parked run's decisions still open go to the new run, and for an author run,
    the messages of its inbox the worker has not handed to an agent too: the answers its owner gave while it was
    parked, which its agent reads in the new run's first turn. ``woken_by``, the answer or the message that resumes
    it, titles the new run of a chat run (``chat_runs.go_on_parked``); any other keeps the parked run's title."""
    kind = (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == parked_id))).scalar_one()
    if kind == "chat":  # a chat run goes on in its thread's session: the run is made as chat_runs makes one
        from evo_agents.hub.server.chat_runs import go_on_parked  # it queues runs through the routes that import this

        return await go_on_parked(conn, parked_id, why, woken_by=woken_by, token_id=user.token_id)
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
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    if kind == "author":
        undelivered = (i.c.run_id == parked_id, i.c.delivered_at.is_(None))
        await conn.execute(update(i).values(run_id=new_id).where(*undelivered))
    shown = f"resumes run #{parked_id} in its session, {why}"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": parked_id})
    await notify_queued(conn, new_id)
    return new_id


async def _lock_run_of(conn: AsyncConnection, project_id: int, plan_id: str | None, decision_id: int):
    """Lock the run decision ``decision_id`` belongs to, and the decision's plan for one with a plan; (run id, its
    state, its kind). A decision of a plan is answered under the plan's dispatch lock, which its run's moves to another
    run (a resume) take too; one without a plan, under its run's row lock, which a resume holds: when the decision
    moved to another run before the lock, the lock is taken again on that run."""
    if plan_id is not None:
        await lock_plan(conn, project_id, plan_id)
    d, r = tables.decisions, tables.runs
    of_decision = select(d.c.run_id).where(d.c.id == decision_id)
    run_id = (await conn.execute(of_decision)).scalar_one()
    while True:
        locked = select(r.c.state, r.c.kind).where(r.c.id == run_id).with_for_update()
        run_state, kind = (await conn.execute(locked)).one()
        now = (await conn.execute(of_decision)).scalar_one()
        if now == run_id:
            return run_id, run_state, kind
        run_id = now


async def _resumes(conn: AsyncConnection, run_id: int, run_state: str, kind: str) -> bool:
    """Whether the answer just recorded resumes run ``run_id``: a parked plan run at its first answer, a parked
    author run or a parked chat run once none of its decisions is open."""
    if run_state != "parked":
        return False
    if kind not in ("author", "chat"):
        return True
    d = tables.decisions
    still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    return (await conn.execute(still_open)).scalar_one() == 0


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
    check_dispatcher(access)
    # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
    # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
    run_id, run_state, kind = await _lock_run_of(conn, access.project_id, plan_id, decision_id)
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
    if category in runs.HUB_DECISION_CATEGORIES:
        return await _answer_hub(conn, user, project, decision_id, run_id, category, chosen, body, via)
    if kind == "local":  # the session takes the answer at its next turn, whatever it does now, but not once it is over
        if run_state == "ended":
            raise HTTPException(
                409, f"run {run_id} of decision {decision_id} is a local run that ended: no session can take an answer"
            )
    elif run_state != "parked" and run_state not in runs.MESSAGE_STATES:
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
    if kind == "local" and run_state == "waiting":  # the session waited for this: it goes on once none is open
        still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
        if (await conn.execute(still_open)).scalar_one() == 0:
            await conn.execute(update(r).values(state="running", waiting_since=None).where(r.c.id == run_id))
    resumes = await _resumes(conn, run_id, run_state, kind)
    text = answer_message(decision_id, category, question, chosen, body.text)
    inbox_run = await _resume(conn, user, run_id, decision_id, text) if resumes else run_id
    i = tables.run_inbox
    left = (
        insert(i).values(run_id=inbox_run, sent_by=user.user_id, body=text, decision_id=decision_id).returning(i.c.id)
    )
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": user.login, "message_id": message_id, "decision_id": decision_id}
    await write_user_message(conn, inbox_run, event)
    target = answer_target(project, plan_id, key, decision_id, run_id, body.option, kind)
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


async def _hub_does(conn: AsyncConnection, user: Principal, decision_id: int, category: str, option: str) -> None:
    """Do what the answer of a decision of the hub's own chose."""
    if category == runs.LOOP_DECISION_CATEGORY:
        from evo_agents.hub.server.loop.stall import (
            answer_loop,
        )  # it hands out runs through the routes that import this

        await answer_loop(conn, user, decision_id, option)
        return
    from evo_agents.hub.server.release_answer import answer_stop  # it queues runs through the routes that import this

    await answer_stop(conn, user, decision_id, option)


async def _answer_hub(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    decision_id: int,
    run_id: int,
    category: str,
    chosen: dict | None,
    body: AnswerIn,
    via: str | None,
) -> Answered:
    """Answer a decision of the hub's own (``runs.HUB_DECISION_CATEGORIES``): of a stopped release cycle, or of a work
    item of the Loop that failed a third time (see the module). The option chosen is done, the decision answered and
    its notification read, audited as decision.answer; no inbox takes it."""
    if chosen is None:
        d = tables.decisions
        options = (await conn.execute(select(d.c.options).where(d.c.id == decision_id))).scalar_one()
        keys = ", ".join(option["key"] for option in options)
        raise HTTPException(422, f"decision {decision_id} of the hub takes one of its options: {keys}")
    await _hub_does(conn, user, decision_id, category, chosen["key"])
    d, n = tables.decisions, tables.notifications
    answered = {"answer_option": body.option, "answer_text": body.text, "answered_by": user.user_id}
    await conn.execute(
        update(d).values(state="answered", answered_at=func.now(), **answered).where(d.c.id == decision_id)
    )
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    target = answer_target(project, None, None, decision_id, run_id, body.option, category)
    access = await project_access(conn, user, project)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target + (f" via={via}" if via is not None else ""),
        project_id=access.project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, run_id)


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
