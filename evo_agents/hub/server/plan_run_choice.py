"""A member's last choice in the Run plan dialog of a project: GET and PUT /v1/projects/{p}/plan-run-choice.

The hub keeps one choice a member and project (plan_run_choices, 0238), so the dialog opens filled in with it: where
the plan runs (``target`` worker, a plan run on a worker, or system, the plan handed to the entry agent of the system
``system`` names, ``plan_systems``), the agent, the runtime, the model, the mode, the timeout in hours and whether each
step gets a session of its own. It never keeps as_me, browser identities or an account: a body naming one is 422. Each
member reads and writes their own and no one else's; the command line neither reads nor writes it. Reading needs a
grant on the project, writing the writer role, as a dispatch does.

A choice is checked when read, against the project as it is then. A field that names what can no longer be used goes
back to its default, and ``defaulted`` lists it: ``system`` for a system deleted, or without an entry agent (the
target is then worker); ``agent`` for an agent deleted or one a run of the project may not go under
(``runs.service.profiles._named``); ``runtime`` for a runtime the project does not register or the agent does not
allow; ``model`` for a model the agent does not allow; ``mode`` and ``timeout_h`` for a value the hub no longer takes.
A member without a choice gets the defaults, with ``saved`` false. A dispatch still checks everything again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, runs, system_spec, tables
from evo_agents.hub.server import system_store
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, by_id, profile_of, profile_select
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models.limits import (
    PROFILE_HELP,
    RUNTIME_HELP,
    ModelName,
    ProfileHandle,
    RuntimeName,
)
from evo_agents.hub.server.runs.service import plugin_runtimes
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.profiles import _named
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_push import SYSTEM, WORKER

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})

Target = Literal["worker", "system"]  # tables_push.TARGETS
PLAN_TIMEOUTS = get_args(runs.PlanTimeout)
DEFAULT_TIMEOUT_H = 4  # PlanRunDispatch.timeout_h's
FIELDS = ("target", "system", "agent", "runtime", "model", "mode", "timeout_h", "session_per_step")
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class PlanRunChoiceIn(BaseModel):
    """What the Run plan dialog keeps of a member's choice; the defaults are a plan run's."""

    model_config = ConfigDict(extra="forbid")

    target: Target = Field(
        WORKER,
        description="worker: a plan run on a worker (POST .../plan-runs); system: the plan handed to the entry agent "
        "of the system ``system`` names (POST .../plans/{plan_id}/system-dispatch)",
    )
    system: str | None = Field(
        None,
        min_length=1,
        max_length=63,
        pattern=system_spec.SLUG_PATTERN,
        description="the slug of a system of the project when target is system; null when it is worker",
    )
    agent: ProfileHandle | None = Field(None, description=PROFILE_HELP)
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    model: ModelName | None = Field(None, description="the model, as its runtime names it; null: the runtime's own")
    mode: runs.Mode = "headless"
    timeout_h: runs.PlanTimeout = Field(DEFAULT_TIMEOUT_H, description="hours of agent time a plan run may take")
    session_per_step: bool = Field(False, description="true: a new agent session for each step of a plan run")

    @model_validator(mode="after")
    def _system_with_target(self) -> PlanRunChoiceIn:
        if (self.target == SYSTEM) != (self.system is not None):
            raise ValueError("name a system when, and only when, target is system")
        return self


class PlanRunChoice(PlanRunChoiceIn):
    """The caller's choice as the hub keeps it, checked against the project now."""

    model_config = ConfigDict(extra="ignore")

    saved: bool = Field(description="whether the caller saved a choice in the project; false: these are the defaults")
    defaulted: list[str] = Field(
        description="the fields of the saved choice that went back to their default, since what they named can no "
        "longer be used: system (deleted, or without an entry agent; target is then worker), agent (deleted, or not "
        "allowed in the project), runtime, model (not allowed, or not registered), mode, timeout_h"
    )
    updated_at: datetime | None = Field(description="when the caller saved it; null when they never did")


async def _default_profile(conn: AsyncConnection, project_id: int) -> Profile | None:
    ap = tables.agent_profiles
    row = (await conn.execute(profile_select().where(ap.c.project_id == project_id, ap.c.is_default))).one_or_none()
    return None if row is None else profile_of(row)


async def _entry_usable(conn: AsyncConnection, project_id: int, slug: str) -> bool:
    system = await system_store.one(conn, project_id, slug)
    if system is None or system.entry_profile_id is None:
        return False
    entry = await by_id(conn, system.entry_profile_id)
    return entry is not None and entry.deleted_at is None


async def checked(conn: AsyncConnection, user: Principal, access: ProjectAccess, row) -> PlanRunChoice:
    """The choice ``row`` of plan_run_choices (None: no choice), each field that names what can no longer be used
    back to its default and listed."""
    if row is None:
        return PlanRunChoice(saved=False, defaulted=[], updated_at=None)
    values = {name: getattr(row, name) for name in FIELDS}
    defaulted = []
    if values["target"] == SYSTEM and not await _entry_usable(conn, access.project_id, values["system"]):
        values.update(target=WORKER, system=None)
        defaulted.append("system")
    profile = None
    if values["agent"] is not None:
        try:
            profile = await _named(conn, user, access, values["agent"])
        except HTTPException:
            values["agent"] = None
            defaulted.append("agent")
    if profile is None:
        profile = await _default_profile(conn, access.project_id)
    permissions = None if profile is None else profile.permissions
    if values["runtime"] != "any":
        try:
            await plugin_runtimes.check(conn, access.project_id, access.name, values["runtime"])
            allowed = profiles.allows(permissions, "runtime", values["runtime"])
        except HTTPException:
            allowed = False
        if not allowed:
            values["runtime"] = "any"
            defaulted.append("runtime")
    if values["model"] is not None and not profiles.allows(permissions, "model", values["model"]):
        values["model"] = None
        defaulted.append("model")
    if values["mode"] not in runs.MODES:
        values["mode"] = "headless"
        defaulted.append("mode")
    if values["timeout_h"] not in PLAN_TIMEOUTS:
        values["timeout_h"] = DEFAULT_TIMEOUT_H
        defaulted.append("timeout_h")
    return PlanRunChoice(**values, saved=True, defaulted=defaulted, updated_at=row.updated_at)


async def _row(conn: AsyncConnection, user_id: int, project_id: int):
    c = tables.plan_run_choices
    query = select(*(c.c[name] for name in FIELDS), c.c.updated_at)
    return (await conn.execute(query.where(c.c.user_id == user_id, c.c.project_id == project_id))).one_or_none()


@router.get("/{project}/plan-run-choice", response_model=PlanRunChoice, responses=REFUSALS)
async def read_plan_run_choice(request: Request, project: ProjectName, user: CurrentUser) -> PlanRunChoice:
    """The caller's last choice in the Run plan dialog of the project, checked against the project now; the defaults
    when they saved none."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        return await checked(conn, user, access, await _row(conn, user.user_id, access.project_id))


@router.put("/{project}/plan-run-choice", response_model=PlanRunChoice, responses=REFUSALS)
async def put_plan_run_choice(
    request: Request, project: ProjectName, body: PlanRunChoiceIn, user: CurrentUser
) -> PlanRunChoice:
    """Keep the caller's choice in the Run plan dialog of the project, in place of the one before; the answer is the
    choice as GET reads it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        c = tables.plan_run_choices
        values = body.model_dump()
        statement = pg_insert(c).values(user_id=user.user_id, project_id=access.project_id, **values)
        statement = statement.on_conflict_do_update(
            constraint="plan_run_choices_pkey", set_={**values, "updated_at": func.now()}
        )
        await conn.execute(statement)
        return await checked(conn, user, access, await _row(conn, user.user_id, access.project_id))
