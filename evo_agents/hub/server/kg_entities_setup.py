"""Turning on the entities step of a project's graph from the web: the one click of the Entities view's empty state.

- GET /v1/kg/{project}/entities/setup: whether the strategy the project's builds follow has the entities step, the
  sink its mapping sends text through and why that sink would take no item, the build queued or running, and whether
  the reader may change it (the writer role).
- POST /v1/kg/{project}/entities/setup {reason}: a writer turns the step on and queues a build. A strategy without the
  step is replaced by ENTITY_STRATEGY with the settings it carries, as ``evo-agents hub kg settings --strategy`` does;
  a strategy that has it is kept, and the click only queues a build. A sink the project's knowledge config does not
  declare as an llm-backend is a 409 and nothing changes: that config is the harness's knowledge.yaml, which only
  ``evo-agents hub kg push`` replaces, so the hub cannot add the sink itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.kg import REFUSALS
from evo_agents.hub.server.kg.models.entities_setup import SetupOut as SetupOut
from evo_agents.hub.server.kg.service import entities_setup as service
from evo_agents.hub.server.kg_settings import MAX_REASON_CHARS
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses=REFUSALS)


class SetupBody(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)


@router.get("/{project}/entities/setup", response_model=SetupOut)
async def get_setup(request: Request, project: ProjectName, user: CurrentUser) -> SetupOut:
    """Whether the project's builds read entities, and what a click would do."""
    return await service.setup(request.app.state.engine, user, project)


@router.post("/{project}/entities/setup", status_code=202, response_model=SetupOut)
async def post_setup(request: Request, project: ProjectName, body: SetupBody, user: CurrentUser) -> SetupOut:
    """Turn the entities step on when it is off, then queue a build that reads the entities."""
    state = request.app.state
    return await service.turn_on(state.engine, state.jobs, user, project, body.reason)
