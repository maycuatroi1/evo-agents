"""Decisions: the questions the agent of a plan run or an author run asks its owner, and the owner's answers.
``docs/notifications.md`` (Decisions) is the design, ``runs`` holds the categories, states and limits, and
``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run, an author run or a chat run
(404 for any other run: a run of one step, a review run, a judge run): a category of ``runs.DECISION_CATEGORIES`` (a
chat run's is ``triage`` alone, and ``triage`` is no other run's; 422 for any other, and for a question of a plan run
or an author run that asks for a credential's value in words, ``admit_ask``), a question, a context in markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the
agent recommends, and optionally why, whether the act it asks about can be undone and how, and the refs of what it
rests on (``decision_models``, which holds the models, 422 for a ref in no form ``decision_asks`` reads), the step it
is about, which must be a step of the run's plan (422 for an author run that has put no plan yet, which has no steps),
and the outputs of the run it carries (``output_ids``, 422 for one of another run;
``output_store``), which every view of the decision lists with the decision's URL of their bytes. The decision keeps the plan the run has when its agent asks, none (``plan_id`` null)
for an author run that has put no plan yet. It answers 201 with the decision, whose id the agent waits for, leaves a
``system`` event in the run's log and notifies the run's owner (``notifications.notify``); the notification of a
decision without a plan names the author run and its title instead. A run has at most MAX_OPEN_DECISIONS open at once
(409). A plan run or an author run whose autonomy card leaves merging (K5) or deploying and releasing (K6) to its
agent gets 409 for a question of category merge or deploy, or a scope question asking leave to do it, and the reason
quotes the card's rule (``admit_ask``, ``autonomy_card.left_to_agent``); a run without a snapshot asks as before.

POST /v1/projects/{p}/decisions/{id}/answer is for the run's owner alone, with a web session or a machine token of
their own: another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/) and the token
of any run that runs as its owner (``run_access``), and someone without a grant on the project 404. A
machine token also gets 403 when the run is on a worker set to take runs dispatched from the web only, held, pinned or
parked there (``runs.web_only_steering``): its answer would steer that worker's agent, or resume the parked run on it.
A decision that is no longer open gets 409. The answer names an option of the decision, gives text of the owner's own
(at most 4 KiB), or both. Under the plan's dispatch lock (for a decision with a plan) and the run's row lock, the
decision is answered, its notification read, and the answer goes to a run's inbox as a message that names the decision
(``run_inbox.decision_id``), with a ``user_message`` event, so the heartbeat counts it and the worker hands it to the
agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked plan run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox;
- a parked author run is resumed so only once the answer leaves none of its decisions open. Until then the answer waits
  in the parked run's inbox, and the run that resumes it (on the last answer, or on a reply in its chat) takes every
  message of that inbox the worker has not handed to an agent, so the agent reads them all in its first turn.

The audit row decision.answer names the decision, its run and the option, never the text.

A credential request (a decision of category credential that carries ``credential_request``,
``evo_agents.hub.credential_requests``) is asked of the run's owner whoever the run's principal reports to, and takes
the hub's options. Its answer may carry the value of the variable it asks for (``AnswerIn.secret``):
``credential_answers`` keeps it as the owner's secret of kind env for the run's project and lets the run run as them
when the option says so, for the run's owner alone, before a parked run is resumed, so the run that resumes it is
leased both. The decision's answer text, and the inbox message, carry the hub's sentence of what was given, by name,
never the value. Before it is asked, the hub looks for the owner's env secret of that variable
(``credential_resolver``): the run leased one, or one the hub may bind under K12 auto, and the hub answers the request
itself, answered by nobody, with a notice to the owner; else the decision's context names the secret, and the option
``found`` binds it when the owner chooses it.

An option the agent marks ``waits`` says not yet (``worker ask --wait-option``): choosing it leaves the decision open
with ``held_at`` set and its notification read, and sends nothing to the agent, so the run goes on waiting, or parked,
for a later answer (``_hold``); words with such an option are 422, since no agent would read them. A decision of
category wait is a wait of the agent's (``run_waits``): the hub answers it when its time or its event comes, and the
person asked may answer it sooner like any other.

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

import logging
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from sqlalchemy import BigInteger, and_, case, false, func, insert, literal, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import Select

from evo_agents.hub import autonomy_card, autonomy_levels, credential_requests, runs, tables
from evo_agents.hub.autonomy_levels import AUTO
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit, credential_answers, credential_resolver, principals, run_access_rules
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.decision_models import AnswerIn as AnswerIn
from evo_agents.hub.server.decision_models import Decision as Decision
from evo_agents.hub.server.decision_models import DecisionAsk as DecisionAsk
from evo_agents.hub.server.decision_models import DecisionList as DecisionList
from evo_agents.hub.server.decision_models import DecisionOption as DecisionOption
from evo_agents.hub.server.decision_models import OptionIn as OptionIn
from evo_agents.hub.server.decision_models import WakeOn as WakeOn
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import decision_link, notify, one_line
from evo_agents.hub.server.output_store import link_decision
from evo_agents.hub.server.output_views import by_decision as outputs_by_decision
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.models import MAX_ID, REFUSALS, RunId
from evo_agents.hub.server.runs.service.controls import web_only_steering
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.views import lock_plan, sees_unplanned, visible_plans
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_OPEN_DECISIONS = 20  # a run's agent asks at most this many before one is answered
QUESTION_IN_MESSAGE = 500  # characters of the question the answer's inbox message repeats
MAX_LIST = 200
MAX_OFFSET = 100_000

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})

DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]


# Reading decisions


def asked_person():
    """Who answers a decision, of a query that joins its run: the person it was asked of, or before schema 0115 a
    gate's decision of autonomy the run's principal and any other the run's owner."""
    d, r = tables.decisions, tables.runs
    gate = and_(d.c.category == runs.AUTONOMY_DECISION_CATEGORY, r.c.principal_id.is_not(None))
    return func.coalesce(d.c.asked_of, case((gate, r.c.principal_id), else_=r.c.dispatched_by))


def _decisions():
    """The columns of Decision but its options as stored, named as its fields: the decision with its project, its
    run's state and owner, who it is asked of, who answered, and the run whose inbox the answer went to (the first
    message naming it)."""
    d, p, r, i = tables.decisions, tables.projects, tables.runs, tables.run_inbox
    owner, answerer, asked = tables.users.alias("o"), tables.users.alias("a"), tables.users.alias("q")
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
        d.c.recommended_reason,
        d.c.reversible,
        d.c.undo,
        d.c.evidence,
        d.c.state,
        owner.c.login.label("owner"),
        asked.c.login.label("asked_of"),
        d.c.answer_option,
        d.c.answer_text,
        answerer.c.login.label("answered_by"),
        answer_run.label("answer_run_id"),
        d.c.asked_at,
        d.c.answered_at,
        d.c.delivered_at,
        d.c.held_at,
        d.c.wake_at,
        d.c.wake_on,
        d.c.credential_request,
    ).select_from(
        d.join(p, p.c.id == d.c.project_id)
        .join(r, r.c.id == d.c.run_id)
        .join(owner, owner.c.id == r.c.dispatched_by)
        .join(asked, asked.c.id == asked_person())
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
            waits=option.get("waits") is True,
        )
        for option in fields.pop("options")
    ]
    recommended = next((option.key for option in shown if option.recommended), None)
    wake_on = fields.pop("wake_on")
    shown_wake = None if wake_on is None else WakeOn(kind=wake_on["kind"], contains=wake_on.get("contains") or {})
    fields["evidence"] = fields["evidence"] or []
    return Decision(**fields, options=shown, recommended=recommended, wake_on=shown_wake)


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


async def _notify_asked(
    conn: AsyncConnection, row, run_id: int, decision_id: int, key: str | None, body, asked: int
) -> None:
    """Tell ``asked``, the person the decision just asked goes to, of it: what it is about, its plan and step, or for
    an author run without a plan, the run and its title, and who it is asked of."""
    r, u = tables.runs, tables.users
    title = (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one()
    login = (await conn.execute(select(u.c.login).where(u.c.id == asked))).scalar_one()
    if row.plan_id is not None:
        about = f"plan {row.plan_id}" + (f", step {key}" if key else "")
    else:
        about = about_run(run_id, row.kind, title)
    who = "Run" if row.kind == "plan" else f"{row.kind.capitalize()} run"
    await notify(
        conn,
        user_id=asked,
        kind="decision",
        project_id=row.project_id,
        run_id=run_id,
        decision_id=decision_id,
        title=f"{who} #{run_id} asks: {one_line(body.question)}",
        body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {row.name}, asked of {login}.",
        link=decision_link(decision_id),
    )


async def store_decision(
    conn: AsyncConnection, run_id: int, row, body: DecisionAsk, *, wake: dict | None = None, quiet: bool = False
) -> Decision:
    """Store the decision ``body`` asks for run ``run_id``, whose row the caller holds locked, in the caller's
    transaction, with the outputs of the run it names, notify the run's owner and log it: ``row`` is what
    ``held_plan_run`` reads, or for a local run (``local_runs``) the same columns. 409 past MAX_OPEN_DECISIONS open,
    422 for a step the plan does not have or an output of another run. A wait (``run_waits``) gives ``wake``, its
    columns wake_at and wake_on, and notifies nobody: it asks nobody anything. So does a credential request the hub
    answers itself (``quiet``, ``_ask_credential``)."""
    d = tables.decisions
    opened = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    if (await conn.execute(opened)).scalar_one() >= MAX_OPEN_DECISIONS:
        raise HTTPException(
            409,
            f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
        )
    key = await _step_of_run(conn, run_id, row, body.step_key)
    # A credential request asks the run's owner for what is theirs alone to give: a value, their account.
    asked = row.dispatched_by if body.credential_request is not None else await principals.asked_of(conn, run_id)
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
            **body.stored_extras(),
            asked_of=asked,
            **(wake or {}),
        )
        .returning(d.c.id)
    )
    decision_id = (await conn.execute(stored)).scalar_one()
    await link_decision(conn, run_id, decision_id, body.output_ids)
    if wake is None and not quiet:
        await _notify_asked(conn, row, run_id, decision_id, key, body, asked)
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


# The kinds of run whose agent the hub holds to asking for a credential with a credential request, and to doing on its
# own what its autonomy card leaves to it: the agents that follow a plan or write one. A chat run asks triage alone, and a local run asks on a route of its own (local_runs).
CREDENTIAL_GATED_KINDS = ("plan", "author")


def admit_ask(run_id: int, kind: str, body: DecisionAsk, card: autonomy_card.Card | None = None) -> None:
    """The gate of a decision the agent of a run of ``kind`` asks, before the hub stores anything of it: 422 for a
    category the run does not ask in (``_in_its_category``), and for a question of a plan run or an author run that
    asks in words for a credential's value (``credential_requests.prose_credential``), whose reason names the command
    that asks it as a credential request; 409 for a question of such a run about a class its autonomy ``card`` leaves to
    the agent (``autonomy_card.left_to_agent``), whose reason quotes the card's rule. A refused question is kept
    nowhere: the reason repeats a variable's name or the card's line at most, and the log line says the run, the
    category and the class alone."""
    _in_its_category(run_id, kind, body.category)
    if kind not in CREDENTIAL_GATED_KINDS or body.credential_request is not None:
        return
    refusal = credential_requests.prose_credential(body.category, body.question, body.context)
    if refusal is not None:
        log.info("decision refused: credential in prose", extra={"run_id": run_id, "category": body.category})
        raise HTTPException(422, f"run {run_id}: {refusal}")
    left = autonomy_card.left_to_agent(card, body.category, body.question)
    if left is not None:
        klass, rule = left
        log.info(
            "decision refused: autonomy leaves it to the agent",
            extra={"run_id": run_id, "category": body.category, "class": klass},
        )
        raise HTTPException(
            409,
            f"run {run_id}: your autonomy card leaves {klass} ({autonomy_levels.CLASS_NAMES[klass]}) to you, so the "
            f"hub takes no question of it; the card's rule: {rule!r}. Do it yourself without asking, and write the "
            'choice, why and how to undo it on a line that starts with "Decision:" in the evidence of the step.',
        )


async def _ask_credential(conn: AsyncConnection, user: Principal, run_id: int, row, body: DecisionAsk) -> Decision:
    """Store credential request ``body`` of run ``run_id`` after the hub looked for what its owner holds already
    (``credential_resolver``): answered by the hub when the run is leased the owner's secret for the variable, or once
    the hub bound it as K12 of the run's principal lets it; else asked of the owner, its context naming what the hub
    found and the command that binds it."""
    request = body.credential_request.model_dump()
    found = await credential_resolver.look(conn, user, run_id, request)
    if found is None or not found.any:
        return await store_decision(conn, run_id, row, body)
    choice = await credential_resolver.k12(conn, run_id)
    answers = credential_resolver.answerable(request) and (
        found.leased or (found.bindable and choice is not None and choice.mode == AUTO)
    )
    if not answers:
        if found.bindable:
            body.options = [OptionIn(**credential_resolver.found_option(found, leased=False)), *body.options]
        view = await store_decision(conn, run_id, row, body)
        context = credential_resolver.with_context(
            body.context, credential_resolver.inbox_context(found, view.id, None if choice is None else choice.mode)
        )
        d = tables.decisions
        await conn.execute(update(d).values(context=context).where(d.c.id == view.id))
        return await decision_view(conn, view.id)
    bound = not found.leased
    view = await store_decision(conn, run_id, row, body, quiet=True)
    if bound:
        name = await credential_resolver.bind_by_hub(conn, row, run_id, view.id, found, choice)
        found.leased = [name]
    option = credential_resolver.found_option(found, leased=True)
    body.options = [OptionIn(**option), *body.options]
    await _answer_by_hub(conn, run_id, row, view, body, found, option, bound=bound)
    return await decision_view(conn, view.id)


async def _answer_by_hub(conn: AsyncConnection, run_id: int, row, view: Decision, body, found, option, *, bound: bool):
    """Answer credential request ``view`` of run ``run_id`` as the hub, with ``option`` naming the owner's secret
    ``found`` leases to the run: answered by nobody, the answer in the run's inbox as an owner's would be, so the worker
    takes the run's leases again, a notice to the owner instead of a decision, and the audit row decision.answer
    ending by=hub."""
    d, i = tables.decisions, tables.run_inbox
    words = credential_resolver.answer_sentence(found, bound=bound)
    await conn.execute(
        update(d)
        .values(
            options=body.stored_options(),
            state="answered",
            answer_option=option["key"],
            answer_text=words,
            answered_at=func.now(),
        )
        .where(d.c.id == view.id)
    )
    text = answer_message(view.id, view.category, view.question, option, words)
    left = insert(i).values(run_id=run_id, sent_by=row.dispatched_by, body=text, decision_id=view.id).returning(i.c.id)
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": "hub", "message_id": message_id, "decision_id": view.id}
    await write_user_message(conn, run_id, event)
    name = found.leased[0]
    await notify(
        conn,
        user_id=row.dispatched_by,
        kind="notice",
        notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
        project_id=row.project_id,
        run_id=run_id,
        details={"decision_id": view.id},
        title=f"Run #{run_id} got {found.env_var} from your secret {name}",
        body=f"{one_line(view.question)}\n\n{words}\n\nThe hub answered credential request #{view.id} itself: "
        "nothing waits on you. Delete or rebind the secret with `evo-agents hub secret` to change what runs get.",
        link=decision_link(view.id),
    )
    target = answer_target(row.name, row.plan_id, view.step_key, view.id, run_id, option["key"], row.kind) + " by=hub"
    await audit.record(
        conn, actor_id=None, token_id=None, action=audit.DECISION_ANSWER, target=target, project_id=row.project_id
    )
    log.info("credential request answered by the hub", extra={"run_id": run_id, "decision_id": view.id, "bound": bound})


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
        card = await principals.card(conn, run_id) if row.kind in CREDENTIAL_GATED_KINDS else None
        admit_ask(run_id, row.kind, body, card)
        if body.credential_request is not None:
            view = await _ask_credential(conn, user, run_id, row, body)
        else:
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
    run its request, a plan run its session per step, and every run its agent profile and version, and whether it runs
    as its owner (``run_access``), with a token of its own."""
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
        r.c.browser_identities,
        r.c.profile_id,
        r.c.profile_version,
        r.c.principal_id,
        r.c.autonomy,
        r.c.as_owner,
        r.c.as_owner_since,
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
    conn: AsyncConnection, user: Principal | None, parked_id: int, why: str, *, woken_by: str | None = None
) -> int:
    """Queue the run that resumes parked run ``parked_id``, whose row the caller holds locked (for a plan run, under
    the plan's lock), and end the parked one done; the new run's id. ``why`` ends the new run's first event, such as
    "as owner answered decision #3". The parked run's decisions still open go to the new run, and for an author run,
    the messages of its inbox the worker has not handed to an agent too: the answers its owner gave while it was
    parked, which its agent reads in the new run's first turn. ``woken_by``, the answer or the message that resumes
    it, titles the new run of a chat run (``chat_runs.go_on_parked``); any other keeps the parked run's title.
    ``user`` is None when the hub resumes it itself, as the reaper, for a wait that woke (``run_waits``). A request that
    carries a run's token resumes none (``run_access_rules.refuse_new_run``)."""
    run_access_rules.refuse_new_run()
    token_id = None if user is None else user.token_id
    kind = (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == parked_id))).scalar_one()
    if kind == "chat":  # a chat run goes on in its thread's session: the run is made as chat_runs makes one
        from evo_agents.hub.server.chat_runs import go_on_parked  # it queues runs through the routes that import this

        return await go_on_parked(conn, parked_id, why, woken_by=woken_by, token_id=token_id)
    reserved = select(func.nextval(func.pg_get_serial_sequence("runs", "id")))
    new_id = (await conn.execute(reserved)).scalar_one()
    # The parked run leaves the active states first: a plan has one active plan run at a time.
    await move_run(
        conn,
        parked_id,
        "parked",
        "done",
        "owner" if user is not None else "reaper",
        reason=f"resumed as #{new_id}",
        token_id=token_id,
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


async def lock_run_of(conn: AsyncConnection, project_id: int, plan_id: str | None, decision_id: int):
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
    conn: AsyncConnection,
    user: Principal,
    project: str,
    decision_id: int,
    body: AnswerIn,
    *,
    via: str | None = None,
    sealer: Sealer | None = None,
) -> Answered:
    """Answer decision ``decision_id`` of ``project`` as ``user``, in the caller's transaction: the checks, the answer,
    its notification read, the inbox message, a parked run resumed and the audit row, as the module's docstring says;
    for a credential request, what the answer gives the run too (``credential_answers``), its value sealed with
    ``sealer``. The answer route and the Telegram channel (``via="telegram"``, which the audit row names) both answer
    through it; it raises the HTTPException the route answers with."""
    access = await readable_decision(conn, user, project, decision_id, None)
    if user.acts_for_run is not None:  # a run's token acts as the owner, but answers no decision (run_access)
        raise HTTPException(
            403,
            f"decision {decision_id} is a question to a member, and a run's token (evr_...) answers no decision of any "
            "run: the owner answers with their own sign-in",
        )
    d, r, u = tables.decisions, tables.runs, tables.users
    query = (
        select(d.c.plan_id, d.c.step_key, u.c.id, u.c.login, r.c.dispatched_by)
        .select_from(d.join(r, r.c.id == d.c.run_id).join(u, u.c.id == asked_person()))
        .where(d.c.id == decision_id, d.c.project_id == access.project_id)
    )
    found = (await conn.execute(query)).one()
    plan_id, key, owner_id, owner = found.plan_id, found.step_key, found.id, found.login
    if owner_id != user.user_id and user.user_id not in await principals.escalated_to(conn, decision_id):
        who = "who dispatched its run" if owner_id == found.dispatched_by else "whom it is asked of"
        raise HTTPException(403, f"only {owner}, {who}, may answer decision {decision_id}")
    check_dispatcher(access)
    # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
    # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
    run_id, run_state, kind = await lock_run_of(conn, access.project_id, plan_id, decision_id)
    locked = select(d.c.state, d.c.category, d.c.question, d.c.options, d.c.credential_request)
    state, category, question, options, request = (
        await conn.execute(locked.where(d.c.id == decision_id).with_for_update())
    ).one()
    if state != "open":
        raise HTTPException(409, f"decision {decision_id} is {state}, not open: it takes no answer any more")
    asked = credential_answers.checked(decision_id, request, body)
    chosen = None
    if asked.option is not None:
        chosen = next((option for option in options if option["key"] == asked.option), None)
        if chosen is None:
            keys = ", ".join(option["key"] for option in options)
            raise HTTPException(422, f"decision {decision_id} has no option {asked.option!r}; its options are {keys}")
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
    if chosen is not None and chosen.get("waits") is True:
        target = answer_target(project, plan_id, key, decision_id, run_id, body.option, kind) + " held"
        return await _hold(conn, user, access.project_id, decision_id, run_id, chosen, body, target, via)
    words = body.text
    if credential_answers.acts(request, asked):  # before a resume, which copies what the run may do
        given = await credential_answers.give(conn, user, sealer, access, project, decision_id, run_id, request, asked)
        words = credential_answers.with_sentence(body.text, given)
    await conn.execute(
        update(d)
        .values(
            state="answered",
            answer_option=asked.option,
            answer_text=words,
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
    text = answer_message(decision_id, category, question, chosen, words)
    inbox_run = await _resume(conn, user, run_id, decision_id, text) if resumes else run_id
    i = tables.run_inbox
    left = (
        insert(i).values(run_id=inbox_run, sent_by=user.user_id, body=text, decision_id=decision_id).returning(i.c.id)
    )
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": user.login, "message_id": message_id, "decision_id": decision_id}
    await write_user_message(conn, inbox_run, event)
    target = answer_target(project, plan_id, key, decision_id, run_id, asked.option, kind)
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


async def _hold(
    conn: AsyncConnection,
    user: Principal,
    project_id: int,
    decision_id: int,
    run_id: int,
    chosen: dict,
    body: AnswerIn,
    target: str,
    via: str | None,
) -> Answered:
    """The person asked chose ``chosen``, an option that waits: the decision stays open with ``held_at`` now, its
    notification is read, nothing goes to the agent and the run goes on waiting (or parked) for a later answer. The
    run's log and the audit row decision.answer (its target ending ``held``) say so. 422 with words of their own: words
    go to the agent, which an option that waits does not reach."""
    if body.text is not None:
        raise HTTPException(
            422,
            f"option {chosen['key']} of decision {decision_id} leaves it open for a later answer, and the agent hears "
            "nothing of it: send your words with another option, or alone",
        )
    d, n = tables.decisions, tables.notifications
    await conn.execute(update(d).values(held_at=func.now()).where(d.c.id == decision_id))
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    shown = (
        f"{user.login} chose {one_line(chosen['label'], 80)} on decision #{decision_id}: it stays open, and the run "
        "waits for a later answer"
    )
    await write_event(conn, run_id, {"text": shown, "decision": {"id": decision_id, "held": chosen["key"]}})
    if via is not None:
        target += f" via={via}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target,
        project_id=project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, run_id)


async def _hub_does(conn: AsyncConnection, user: Principal, decision_id: int, category: str, option: str) -> None:
    """Do what the answer of a decision of the hub's own chose; a gate of autonomy reads the answer at its next pass."""
    if category == runs.AUTONOMY_DECISION_CATEGORY:
        return
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
        answered = await answer_decision(conn, user, project, decision_id, body, sealer=request.app.state.sealer)
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
