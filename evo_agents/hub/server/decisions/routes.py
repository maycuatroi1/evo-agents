"""The routes of decisions: a project's list and one decision (``reads``), its answer (``answers``), and the question
the worker of a run asks (``asks``). Each reads its scope and hands the request to the service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from evo_agents.hub import runs
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.decisions.models import (
    MAX_LIST,
    MAX_OFFSET,
    AnswerIn,
    Decision,
    DecisionAsk,
    DecisionId,
    DecisionList,
)
from evo_agents.hub.server.decisions.service import answers, asks, reads
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans.models import SINK_HEADER
from evo_agents.hub.server.runs.models import MAX_ID, REFUSALS, RunId
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

Granted = Annotated[Scope, Depends(reads.GRANTED, scope="function")]
Reading = Annotated[Scope, Depends(reads.READERS, scope="function")]


@router.get(
    "/{project}/decisions",
    response_model=DecisionList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_decisions(
    project: ProjectName,
    scope: Granted,
    state: Annotated[
        list[Literal[runs.DECISION_STATES]],  # pyright: ignore[reportInvalidTypeForm]
        Query(description="any of these states; repeat it"),
    ] = [],  # noqa: B006
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    plan_id: Annotated[str | None, Query(pattern=PLAN_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> DecisionList:
    """The project's decisions, newest first, of the runs the caller may read."""
    return await reads.page(scope.conn, scope.user, scope.access, state, run_id, plan_id, limit, offset, sink)


@router.get(
    "/{project}/decisions/{decision_id}",
    response_model=Decision,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_decision(
    project: ProjectName,
    decision_id: DecisionId,
    scope: Reading,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> Decision:
    return await reads.shown(scope, decision_id, sink)


@router.post(
    "/{project}/decisions/{decision_id}/answer",
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def answer(project: ProjectName, decision_id: DecisionId, body: AnswerIn, scope: Reading) -> Decision:
    """Answer a decision of a run one dispatched: the answer goes to the agent through the run's inbox, and a parked
    run is resumed on its worker in its session."""
    return (await answers.answer(scope, decision_id, body)).decision


@worker_router.post(
    "/runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: DecisionAsk, user: CurrentUser) -> Decision:
    """Ask the owner of a plan run, an author run or a chat run this worker holds a decision, and notify them."""
    return await asks.ask(request.app.state.engine, user, run_id, body)
