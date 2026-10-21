"""The routes of a project's Loop (plan loop-engine): its state, the reset of its breaker (criterion a7), and the
project's decision board (criterion a8).

- GET /v1/projects/{p}/loop: whether the Loop is on, its breaker (armed or tripped, why and when, who reset it last
  and when), its cursor on hub_events, when its last tick ended, and its work items by state; for anyone with a grant
  on the project.
- POST /v1/projects/{p}/loop/resume: an admin of the project resets a tripped breaker (``breaker.resume``): the Loop
  reads its events again and hands out runs from its next tick, queued now. 403 for anyone else, 409 when the breaker
  has not tripped.

- GET /v1/projects/{p}/decision-board: everything of the project that waits for a person, the three gates first, then
  what the Loop's chain waits on (``board``); for anyone with a grant on the project, who sees what they may read, as
  each object's own route shows it (a canvas's under its label, a decision of a run whose plan they read), through
  the sink X-Evo-Sink names, else the project's hub sink.

Whether the Loop is on is the policy's ``loop.enabled`` (PUT /v1/projects/{p}/policy); nothing here hands out a run,
and no route lets an agent hand itself work through the Loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.loop import board
from evo_agents.hub.server.loop import breaker as loop_breaker
from evo_agents.hub.server.loop.board_items import DecisionBoard
from evo_agents.hub.server.loop.breaker import BREAKER_STATES
from evo_agents.hub.server.loop.model import STATES
from evo_agents.hub.server.plans import SINK_HEADER
from evo_agents.hub.server.projects import project_access
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


async def _view(conn, project_id: int, project: str) -> ProjectLoop:
    return ProjectLoop(project=project, **await loop_breaker.status(conn, project_id))


@router.get("/{project}/loop", response_model=ProjectLoop, responses=REFUSALS)
async def show_loop(request: Request, project: ProjectName, user: CurrentUser) -> ProjectLoop:
    """The project's Loop: on or off, its breaker, its cursor and its work items by state."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the Loop of project {access.name} needs a grant on it")
        return await _view(conn, access.project_id, access.name)


@router.post(
    "/{project}/loop/resume",
    response_model=ProjectLoop,
    responses={code: {"model": ErrorBody} for code in (403, 404, 409)},
)
async def resume_loop(request: Request, project: ProjectName, user: CurrentUser) -> ProjectLoop:
    """Reset the tripped breaker of the project's Loop (the admin role on the project)."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await loop_breaker.resume(conn, user, access)
        view = await _view(conn, access.project_id, access.name)
    log.info("loop resumed", extra={"project": project, "login": user.login})
    return view


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
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the decision board of project {access.name} needs a grant on it")
        return await board.gather(conn, access, sink)
