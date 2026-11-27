"""Turning on the entities step of a project's graph (``evo_agents.hub.server.kg_entities_setup``): what the step
reads with now, and the one transaction of a writer's click, which sets ENTITY_STRATEGY when the step is off and
queues a build."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_settings as store
from evo_agents.hub.server import audit, project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.entities_setup import (
    DEFAULT_REASON,
    ENTITY_STRATEGY,
    IN_FLIGHT,
    NO_CONFIG,
    SetupOut,
)
from evo_agents.hub.server.kg.repository.graph import find_builds, knowledge
from evo_agents.hub.server.kg.service.graph import queue
from evo_agents.hub.server.kg.service.settings import may_change
from evo_agents.hub.server.kg.service.web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg import strategy as kg_strategy
from evo_agents.kg.entities import sink_problem
from evo_agents.schema.policy import Policy


def _mapping(tuning, step_on: bool) -> dict:
    """The entities mapping a build reads with: the project's own once the step is on, else ENTITY_STRATEGY's."""
    if step_on:
        return tuning.entities or {}
    return kg_strategy.builtins()[ENTITY_STRATEGY].settings.get("entities") or {}


async def _sink_problem(conn: AsyncConnection, project_id: int, project: str, sink: str | None) -> str | None:
    if sink is None:
        return None
    config = await knowledge(conn, project_id)
    if config is None:
        return NO_CONFIG.format(project=project)
    found = sink_problem(Policy(project, config), sink)
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
        can_change=holds(access.role, "writer"),
        building=next((b for b in builds if b.status in IN_FLIGHT), None),
        queued=None,
    )


async def setup(engine: AsyncEngine, user, project: str) -> SetupOut:
    """Whether the project's builds read entities, and what a click would do."""
    async with UnitOfWork(engine) as conn:
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


async def turn_on(engine: AsyncEngine, jobs, user, project: str, reason: str | None) -> SetupOut:
    """Turn the entities step on when it is off, then queue a build that reads the entities, through the app's
    ``jobs``."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
        before = await _state(conn, access)
        if before.sink_problem is not None:
            raise HTTPException(409, before.sink_problem)
        if not before.step_on:
            await _turn_on(conn, user, access, reason or DEFAULT_REASON)
        _, queued = await queue(jobs, conn, access.project_id, project, user.user_id)
        if queued:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_BUILD, target=project
            )
        after = await _state(conn, access)
    return after.model_copy(update={"queued": queued})
