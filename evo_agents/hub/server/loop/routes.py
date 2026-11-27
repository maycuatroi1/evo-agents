"""The routes of a project's Loop (plan loop-engine): its state, the reset of its breaker (criterion a7), and the
project's decision board (criterion a8).

- GET /v1/projects/{p}/loop: whether the Loop is on, its breaker (armed or tripped, why and when, who reset it last
  and when), its cursor on hub_events, when its last tick ended, and its work items by state; for anyone with a grant
  on the project.
- POST /v1/projects/{p}/loop/resume: an admin of the project resets a tripped breaker (``breaker.resume``): the Loop
  reads its events again and hands out runs from its next tick, queued now. 403 for anyone else, 409 when the breaker
  has not tripped.

- GET /v1/projects/{p}/loop/items: its work items, the newest first, of a state, a kind, a goal, a problem or an opportunity when asked,
  each with where it stands and why, the codes of its ancestors and the problem or opportunity it goes from (``items``); GET
  .../loop/items/{id}: one with its chain, the stages of its path from the problem and the goal to the release, and its
  ledger (``chain``); GET .../loop/goals: each goal's metric
  measured now with its work items by state. For anyone with a grant on the project, who sees each ancestor as its own
  route shows it, through the sink X-Evo-Sink names, else the project's hub sink.

- GET /v1/projects/{p}/decision-board: everything of the project that waits for a person, the three gates first, then
  what the Loop's chain waits on (``board``); for anyone with a grant on the project, who sees what they may read, as
  each object's own route shows it (a canvas's under its label, a decision of a run whose plan they read), through
  the sink X-Evo-Sink names, else the project's hub sink.

- POST /v1/projects/{p}/loop/adopt: a plan of the project becomes a deliver of the Loop for the use cases of a goal that
  aims at an SRS document (``adopt``; an admin of the project, or the writer who owns the goal).

- GET /v1/projects/{p}/goals/{ref}/baseline: the baseline of a goal that aims at an SRS document (plan srs-backlog-loop,
  criterion a5): each approved use case of its scope as delivered, undelivered or unknown, with the evidence the
  knowledge graph gives (``baseline``); for anyone with a grant on the project. POST .../baseline/fix sets use cases by
  code, by heading or the unknown ones as a whole, and POST .../baseline/confirm confirms it once (409 the second time),
  as an admin of the project (403 for anyone else, and for the agent of a run); 409 for a goal with no SRS document.
  Until it is confirmed the Loop makes no build for the goal.

Whether the Loop is on is the policy's ``loop.enabled`` (PUT /v1/projects/{p}/policy); nothing here hands out a run,
and no route lets an agent hand itself work through the Loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from pydantic import BaseModel, Field

from evo_agents.hub import goals, opportunities, problems
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import delivery_evidence
from evo_agents.hub.server.loop import adopt, baseline, items, service
from evo_agents.hub.server.loop.board_items import DecisionBoard
from evo_agents.hub.server.loop.breaker import BREAKER_STATES
from evo_agents.hub.server.loop.chain import LoopItemChain
from evo_agents.hub.server.loop.items import LoopGoal, LoopItems
from evo_agents.hub.server.loop.model import KINDS, STATES
from evo_agents.hub.server.plans.models import SINK_HEADER
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["loop"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


class ProjectLoop(BaseModel):
    """A project's Loop as it stands."""

    project: str
    enabled: bool = Field(description="the policy's loop.enabled: the Loop ticks and makes work items")
    breaker: Literal[BREAKER_STATES] = Field(
        description="tripped: too many work items in an hour; the Loop reads no event and hands out no run"
    )
    breaker_reason: str | None
    tripped_at: datetime | None
    resumed_by: str | None = Field(description="the admin who reset the breaker last")
    resumed_at: datetime | None = Field(description="the work items of the hour are counted from then")
    cursor: int = Field(description="the last row of the project's hub_events the Loop read")
    ticked_at: datetime | None
    items: dict[Literal[STATES], int] = Field(description="its work items by state")


@router.get("/{project}/loop", response_model=ProjectLoop, responses=REFUSALS)
async def show_loop(request: Request, project: ProjectName, user: CurrentUser) -> ProjectLoop:
    """The project's Loop: on or off, its breaker, its cursor and its work items by state."""
    return ProjectLoop(**await service.read_loop(request.app.state.engine, user, project))


@router.post(
    "/{project}/loop/resume",
    response_model=ProjectLoop,
    responses={code: {"model": ErrorBody} for code in (403, 404, 409)},
)
async def resume_loop(request: Request, project: ProjectName, user: CurrentUser) -> ProjectLoop:
    """Reset the tripped breaker of the project's Loop (the admin role on the project)."""
    view = ProjectLoop(**await service.resume(request.app.state.engine, user, project))
    log.info("loop resumed", extra={"project": project, "login": user.login})
    return view


@router.get("/{project}/loop/items", response_model=LoopItems, responses=REFUSALS)
async def loop_items(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Literal[STATES] | None = None,
    kind: Literal[KINDS] | None = None,
    goal: Annotated[str | None, Query(pattern=goals.REF, description="a goal's code or alias")] = None,
    open: Annotated[bool, Query(description="only the work items not done or cancelled")] = False,  # noqa: A002
    limit: Annotated[int, Query(ge=1, le=items.MAX_ITEMS)] = items.DEFAULT_ITEMS,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
    problem: Annotated[
        str | None, Query(pattern=problems.REF, description="a problem's code: the work items that go from it")
    ] = None,
    opportunity: Annotated[
        str | None,
        Query(pattern=opportunities.REF, description="an opportunity's code: the work items that go from it"),
    ] = None,
) -> LoopItems:
    """The work items of the project's Loop, the newest first: where each stands and why, and its ancestors."""
    found = {"state": state, "kind": kind, "goal": goal, "open_only": open, "limit": limit, "problem": problem}
    found["opportunity"] = opportunity
    return await service.read_items(request.app.state.engine, user, project, sink, **found)


@router.get("/{project}/loop/items/{item_id}", response_model=LoopItemChain, responses=REFUSALS)
async def loop_item(
    request: Request,
    project: ProjectName,
    item_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> LoopItemChain:
    """A work item of the project's Loop with its chain from the goal to the release, and what happened to it."""
    return await service.read_item(request.app.state.engine, user, project, item_id, sink)


@router.get("/{project}/loop/goals", response_model=list[LoopGoal], responses=REFUSALS)
async def loop_goals(request: Request, project: ProjectName, user: CurrentUser) -> list[LoopGoal]:
    """Each goal of the project with its metric measured now against its target, and its work items by state."""
    return await service.read_goals(request.app.state.engine, user, project)


@router.get("/{project}/decision-board", response_model=DecisionBoard, responses=REFUSALS)
async def decision_board(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> DecisionBoard:
    """Everything of the project that waits for a person: the goals, the canvases and the policy (the three gates),
    then the Loop's exceptions and the SRS versions and settle chats its chain waits on; each with since when, who may
    decide it and the page where it is decided."""
    return await service.read_board(request.app.state.engine, user, project, sink)


BASELINE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.get("/{project}/goals/{ref}/baseline", response_model=baseline.SrsBaseline, responses=BASELINE_REFUSALS)
async def show_baseline(
    request: Request, project: ProjectName, ref: Annotated[str, Path(pattern=goals.REF)], user: CurrentUser
) -> baseline.SrsBaseline:
    """The baseline of a goal that aims at an SRS document: what the hub proposes of each approved use case, with its
    evidence, and whether an admin confirmed it."""
    engine = request.app.state.engine
    access, goal_id, code, codes = await service.baseline_of(engine, user, project, ref)
    found, why = await delivery_evidence(request.app.state, user, project, codes)
    return await service.read_baseline(engine, access, goal_id, code, found, why)


@router.post("/{project}/goals/{ref}/baseline/fix", response_model=baseline.SrsBaseline, responses=BASELINE_REFUSALS)
async def fix_baseline(
    request: Request,
    project: ProjectName,
    ref: Annotated[str, Path(pattern=goals.REF)],
    body: baseline.BaselineFix,
    user: CurrentUser,
) -> baseline.SrsBaseline:
    """Set use cases of the baseline to delivered, undelivered or unknown by code, by heading, or the unknown ones as a
    whole (an admin of the project); with ``cancel_builds``, also a use case a build of the Loop holds, cancelling that
    build so no author run of it is handed out again. Answers the baseline as it stands, with the builds cancelled."""
    engine = request.app.state.engine
    access, goal_id, code, codes = await service.baseline_of(engine, user, project, ref, writing=True)
    found, why = await delivery_evidence(request.app.state, user, project, codes)
    return await service.fix_baseline(engine, user, access, goal_id, code, body, found, why)


@router.post(
    "/{project}/goals/{ref}/baseline/confirm", response_model=baseline.SrsBaseline, responses=BASELINE_REFUSALS
)
async def confirm_baseline(
    request: Request, project: ProjectName, ref: Annotated[str, Path(pattern=goals.REF)], user: CurrentUser
) -> baseline.SrsBaseline:
    """Confirm the baseline of a goal, once (an admin of the project): the Loop then makes builds for its backlog."""
    engine = request.app.state.engine
    access, goal_id, code, codes = await service.baseline_of(engine, user, project, ref, writing=True)
    found, why = await delivery_evidence(request.app.state, user, project, codes)
    return await service.confirm_baseline(engine, user, access, goal_id, code, found, why)


@router.post(
    "/{project}/loop/adopt", response_model=adopt.LoopAdopted, responses=BASELINE_REFUSALS | {422: {"model": ErrorBody}}
)
async def adopt_plan(
    request: Request, project: ProjectName, body: adopt.LoopAdopt, user: CurrentUser
) -> adopt.LoopAdopted:
    """Adopt a plan of the project as a deliver for use cases of a goal's document (an admin of the project, or the
    writer who owns the goal): the use cases are being delivered, and delivered at once for a plan already completed."""
    return await service.adopt_plan(request.app.state.engine, user, project, body)
