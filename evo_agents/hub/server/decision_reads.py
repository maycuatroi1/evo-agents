"""Reading decisions, out of ``decisions``, which asks and answers them.

GET /v1/projects/{p}/decisions lists a project's decisions, newest first, filtered by state, run and plan, a page at a
time, and GET .../decisions/{id} shows one, to the readers of the decision's run, as ``views.readable_run`` reads it:
the readers of its plan (``views.visible_plans``), and for an author run that has put no plan, those who read the
project's runs on no plan (``views.sees_unplanned``).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from sqlalchemy import and_, case, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import decision_channels, waiting_on
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.decision_models import Decision, DecisionList, DecisionOption, WakeOn
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.member_channels import effective_label
from evo_agents.hub.server.output_views import by_decision as outputs_by_decision
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.service.views import sees_unplanned, visible_plans
from evo_agents.hub.server.security import CurrentUser, Principal

MAX_LIST = 200
MAX_OFFSET = 100_000

router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})

DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]


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
        d.c.answered_via,
        answer_run.label("answer_run_id"),
        d.c.asked_at,
        d.c.answered_at,
        d.c.delivered_at,
        d.c.held_at,
        d.c.wake_at,
        d.c.wake_on,
        d.c.credential_request,
        d.c.approval,
        d.c.label.label("label_given"),
        p.c.default_label,
        p.c.levels,
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
    given, default_label, levels = fields.pop("label_given"), fields.pop("default_label"), fields.pop("levels")
    label = effective_label(given, default_label, levels)
    return Decision(
        **fields,
        options=shown,
        recommended=recommended,
        wake_on=shown_wake,
        label=label,
        label_given=given is not None,
    )


async def decision_view(conn: AsyncConnection, decision_id: int, plans: Collection[str] | None = None) -> Decision:
    """Decision ``decision_id`` as every view shows it; ``plans`` are those its reader reads (``waiting_on.fill``)."""
    row = (await conn.execute(_decisions().where(tables.decisions.c.id == decision_id))).one()
    (view,) = await with_outputs(conn, [_decision(row)], plans)
    return view


async def with_outputs(
    conn: AsyncConnection, decisions: list[Decision], plans: Collection[str] | None = None
) -> list[Decision]:
    """``decisions``, each with the outputs it carries, where its message stands on each channel
    (``decision_channels``), and a wait with what it waits for (``waiting_on.fill``, with the plans the reader
    reads)."""
    await waiting_on.fill(conn, decisions, plans)
    await decision_channels.fill(conn, decisions)
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
    state: Annotated[
        list[Literal[runs.DECISION_STATES]],  # pyright: ignore[reportInvalidTypeForm]
        Query(description="any of these states; repeat it"),
    ] = [],  # noqa: B006
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
        page = await with_outputs(conn, [_decision(row) for row in (await conn.execute(query)).all()], plans)
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
        access = await readable_decision(conn, user, project, decision_id, sink)
        return await decision_view(conn, decision_id, await visible_plans(conn, access, sink))
