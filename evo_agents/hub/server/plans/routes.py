"""The routes of the plans of a project (``/v1/projects/{project}/plans``). Each handler passes the request's parts
and the app's engine to the service, which opens the transaction and checks the caller (``service.READERS``,
``service.WRITERS``); a write the service refuses with a ``PlanError`` is answered with its body (``service.refusal``).
The MCP tools (``evo_agents.hub.server.mcp``) call these handlers as functions."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request

from evo_agents.hub.handoff import DEFAULT_BUDGET_TOKENS, MAX_BUDGET_TOKENS, MIN_BUDGET_TOKENS
from evo_agents.hub.plan_diff import DEFAULT_CONTEXT, MAX_CONTEXT
from evo_agents.hub.plans import AREAS
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.plans import READ_REFUSALS, REFUSALS, Written
from evo_agents.hub.server.plans import service as plans
from evo_agents.hub.server.plans.models import (
    REVISION_CONFLICT,
    SINK_HEADER,
    Plan,
    PlanAbandon,
    PlanComplete,
    PlanDiff,
    PlanError,
    PlanHandoff,
    PlanId,
    PlanPatch,
    PlanPut,
    PlanReopen,
    PlanSummary,
    Revision,
    RevisionBody,
)
from evo_agents.hub.server.security import CurrentUser

__all__ = ["REVISION_CONFLICT", "PlanComplete", "PlanPatch", "router"]

router = APIRouter(prefix="/v1/projects/{project}/plans", tags=["plans"], responses={401: {"model": ErrorBody}})

Sink = Annotated[str | None, Header(alias=SINK_HEADER)]


@router.get("", response_model=list[PlanSummary], responses={**READ_REFUSALS, **PAGED})
async def list_plans(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    area: Literal[AREAS] | None = None,  # pyright: ignore[reportInvalidTypeForm]
    sink: Sink = None,
    page: Paging = WHOLE,
) -> list[PlanSummary]:
    return await plans.listed(request.app.state.engine, user, project, area, sink, page)


@router.get("/{plan_id}", response_model=Plan, responses=READ_REFUSALS)
async def show(request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, sink: Sink = None) -> dict:
    return await plans.shown(request.app.state.engine, user, project, plan_id, sink)


@router.get("/{plan_id}/revisions", response_model=list[Revision], responses=READ_REFUSALS)
async def history(
    request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, sink: Sink = None
) -> list[Revision]:
    return await plans.history(request.app.state.engine, user, project, plan_id, sink)


@router.get("/{plan_id}/revisions/{revision}", response_model=RevisionBody, responses=READ_REFUSALS)
async def show_revision(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    revision: Annotated[int, Path(ge=1)],
    user: CurrentUser,
    sink: Sink = None,
) -> RevisionBody:
    return await plans.revision_body(request.app.state.engine, user, project, plan_id, revision, sink)


@router.get("/{plan_id}/diff", response_model=PlanDiff, responses=READ_REFUSALS)
async def diff(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    user: CurrentUser,
    from_revision: Annotated[int, Query(alias="from", ge=1, description="the older revision")],
    to_revision: Annotated[int, Query(alias="to", ge=1, description="the newer revision")],
    context: Annotated[int, Query(ge=0, le=MAX_CONTEXT, description="unchanged lines around a change")] = (
        DEFAULT_CONTEXT
    ),
    sink: Sink = None,
) -> PlanDiff:
    """The lines that changed from one revision of the plan to another, as their git copies read; 404 when
    either revision is not one the caller can see."""
    engine = request.app.state.engine
    return await plans.diff(engine, user, project, plan_id, (from_revision, to_revision), context, sink)


@router.get("/{plan_id}/handoff", response_model=PlanHandoff, responses=READ_REFUSALS)
async def handoff(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    user: CurrentUser,
    step: Annotated[str | None, Query(max_length=200, description="a step: its dependencies' evidence first")] = None,
    budget_tokens: Annotated[
        int, Query(ge=MIN_BUDGET_TOKENS, le=MAX_BUDGET_TOKENS, description="the most tokens the text may take")
    ] = DEFAULT_BUDGET_TOKENS,
    sink: Sink = None,
) -> PlanHandoff:
    """What a new agent session needs to take the plan up, built from the plan as the hub holds it: its goal, the
    steps done with their commits, Decision lines and evidence, the steps not done, within ``budget_tokens``
    (``evo_agents.hub.handoff``). Read under the rule of GET .../plans/{plan_id}; 404 for a step the plan lacks."""
    return await plans.handoff(request.app.state.engine, user, project, plan_id, step, budget_tokens, sink)


@router.put("/{plan_id}", response_model=Written, responses=REFUSALS)
async def put(request: Request, project: ProjectName, plan_id: PlanId, payload: PlanPut, user: CurrentUser):
    """Create the plan, or replace the revision ``if_revision`` names with ``body``."""
    try:
        return await plans.put(request.app.state.engine, user, project, plan_id, payload)
    except PlanError as exc:
        return plans.refusal(request, exc)


@router.patch("/{plan_id}", response_model=Written, responses=REFUSALS)
async def patch(request: Request, project: ProjectName, plan_id: PlanId, payload: PlanPatch, user: CurrentUser):
    """Set keys of one item, as ``evo harness step``, ``repo``, ``debt`` and ``question`` do in a file."""
    try:
        return await plans.patch(request.app.state.engine, user, project, plan_id, payload)
    except PlanError as exc:
        return plans.refusal(request, exc)


@router.post("/{plan_id}/complete", response_model=Written, responses=REFUSALS)
async def complete(
    request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, payload: PlanComplete | None = None
):
    """Move the plan to the completed area; refused while a step is not done, as evo-cli's complete_plan, unless
    ``mark_steps_done`` marks those steps done in the same revision."""
    try:
        return await plans.complete(request.app.state.engine, user, project, plan_id, payload)
    except PlanError as exc:
        return plans.refusal(request, exc)


@router.post("/{plan_id}/abandon", response_model=Written, responses=REFUSALS)
async def abandon(request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, payload: PlanAbandon):
    """Move an active or completed plan to the completed area as abandoned, or superseded when ``superseded_by``
    names the plan that replaces it. The date and reason are kept on the plan; no step changes status. A plan that is
    abandoned already is left as it is."""
    try:
        return await plans.abandon(request.app.state.engine, user, project, plan_id, payload)
    except PlanError as exc:
        return plans.refusal(request, exc)


@router.post("/{plan_id}/reopen", response_model=Written, responses=REFUSALS)
async def reopen(
    request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, payload: PlanReopen | None = None
):
    """Put a completed plan back in the active area with status active, an abandoned or superseded one without its
    abandon keys; an active plan that is not abandoned is a 409. A write that leaves a step open on a completed plan
    reopens it too (``_settle``); this reopens one whose steps are all done as well."""
    try:
        return await plans.reopen(request.app.state.engine, user, project, plan_id, payload)
    except PlanError as exc:
        return plans.refusal(request, exc)
