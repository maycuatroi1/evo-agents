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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_settings as store
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, project_archive
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.kg import REFUSALS, Build, find_builds, queue
from evo_agents.hub.server.kg_settings import MAX_REASON_CHARS, may_change
from evo_agents.hub.server.kg_web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import strategy as kg_strategy
from evo_agents.kg.entities import sink_problem
from evo_agents.kg.policy import Policy

ENTITY_STRATEGY = "entity-graph-api"  # the built-in prose strategy whose model the build calls itself, on the hub too
DEFAULT_REASON = "entity graph view"
IN_FLIGHT = ("queued", "running")
NO_CONFIG = "project {project} has no knowledge config on the hub yet: `evo-agents hub kg push` sends it first"

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses=REFUSALS)


class SetupOut(BaseModel):
    strategy: str | None = Field(description="the strategy the project's builds follow; null for none")
    step_on: bool = Field(description="whether that strategy has the entities step")
    turns_on: str = Field(description="the strategy a writer's click sets while the step is off")
    sink: str | None = Field(description="the sink the entities mapping sends text through; null for a local model")
    sink_problem: str | None = Field(description="why that sink would take no item, as a build says; null when it can")
    can_change: bool = Field(description="whether the reader may turn the step on and queue a build (the writer role)")
    building: Build | None = Field(description="the newest build queued or running")
    queued: bool | None = Field(None, description="POST only: false when a build was waiting already")


class SetupBody(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)


def _mapping(tuning, step_on: bool) -> dict:
    """The entities mapping a build reads with: the project's own once the step is on, else ENTITY_STRATEGY's."""
    if step_on:
        return tuning.entities or {}
    return kg_strategy.builtins()[ENTITY_STRATEGY].settings.get("entities") or {}


async def _sink_problem(conn: AsyncConnection, project_id: int, project: str, sink: str | None) -> str | None:
    if sink is None:
        return None
    configs = tables.kg_configs
    knowledge = (await conn.execute(select(configs.c.knowledge).where(configs.c.project_id == project_id))).scalar()
    if knowledge is None:
        return NO_CONFIG.format(project=project)
    found = sink_problem(Policy(project, knowledge), sink)
    if found is None:
        return None
    return f"{found}: add it (kind llm-backend) to the harness's knowledge.yaml and run `evo-agents hub kg push`"


async def _state(conn: AsyncConnection, access) -> SetupOut:
    tuning = await store.read(conn, access.project_id)
    step_on = "entities" in kg_strategy.steps_of(tuning.strategy)
    sink = _mapping(tuning, step_on).get("sink")
    builds = await find_builds(conn, access.project_id, limit=5)
    return SetupOut(
        strategy=tuning.strategy,
        step_on=step_on,
        turns_on=ENTITY_STRATEGY,
        sink=sink,
        sink_problem=await _sink_problem(conn, access.project_id, access.name, sink),
        can_change=has_role(access.role, "writer"),
        building=next((b for b in builds if b.status in IN_FLIGHT), None),
        queued=None,
    )


@router.get("/{project}/entities/setup", response_model=SetupOut)
async def get_setup(request: Request, project: ProjectName, user: CurrentUser) -> SetupOut:
    """Whether the project's builds read entities, and what a click would do."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        return await _state(conn, access)


async def _turn_on(conn: AsyncConnection, user, access, reason: str) -> None:
    chosen = kg_strategy.builtins()[ENTITY_STRATEGY]
    values = {**chosen.settings, "strategy": chosen.name}
    _, _, made = await store.change(
        conn, access.project_id, values, user_id=user.user_id, run_id=user.acts_for_run, reason=reason, figures=None
    )
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_SETTINGS,
        target=f"{access.name} change={made} kind=set",
        project_id=access.project_id,
    )


@router.post("/{project}/entities/setup", status_code=202, response_model=SetupOut)
async def post_setup(request: Request, project: ProjectName, body: SetupBody, user: CurrentUser) -> SetupOut:
    """Turn the entities step on when it is off, then queue a build that reads the entities."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
        before = await _state(conn, access)
        if before.sink_problem is not None:
            raise HTTPException(409, before.sink_problem)
        if not before.step_on:
            await _turn_on(conn, user, access, body.reason or DEFAULT_REASON)
        _, queued = await queue(request, conn, access.project_id, project, user.user_id)
        if queued:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_BUILD, target=project
            )
        after = await _state(conn, access)
    return after.model_copy(update={"queued": queued})
