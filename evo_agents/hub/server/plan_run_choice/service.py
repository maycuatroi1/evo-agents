"""What the Run plan dialog reads and keeps of a member's choice (``read``, ``keep``), each field checked against the
project as it is now (``checked``). Reading needs the project (``project_access``), keeping the role a dispatch needs
(``runs.service.dispatch.check_dispatcher``)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import profiles, runs
from evo_agents.hub.server import system_store
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plan_run_choice.models import (
    DEFAULT_TIMEOUT_H,
    FIELDS,
    PLAN_TIMEOUTS,
    PlanRunChoice,
    PlanRunChoiceIn,
)
from evo_agents.hub.server.plan_run_choice.repository import STORE
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service import plugin_runtimes
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.profiles import profile_named
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_push import SYSTEM, WORKER


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
        return PlanRunChoice(saved=False, defaulted=[], updated_at=None)  # pyright: ignore[reportCallIssue]  # each field has its default
    values = {name: getattr(row, name) for name in FIELDS}
    defaulted = []
    if values["target"] == SYSTEM and not await _entry_usable(conn, access.project_id, values["system"]):
        values.update(target=WORKER, system=None)
        defaulted.append("system")
    profile = None
    if values["agent"] is not None:
        try:
            profile = await profile_named(conn, user, access, values["agent"])
        except HTTPException:
            values["agent"] = None
            defaulted.append("agent")
    if profile is None:
        profile = await STORE.default_profile(conn, access.project_id)
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


async def read(engine: AsyncEngine, user: Principal, project: str) -> PlanRunChoice:
    """The caller's last choice in the project, checked against the project now; the defaults when they saved none."""
    async with engine.connect() as conn:
        access = await project_access(conn, user, project)
        return await checked(conn, user, access, await STORE.row(conn, user.user_id, access.project_id))


async def keep(engine: AsyncEngine, user: Principal, project: str, body: PlanRunChoiceIn) -> PlanRunChoice:
    """Keep the caller's choice in the project, in place of the one before; the choice as ``read`` reads it."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await STORE.keep(conn, user.user_id, access.project_id, body.model_dump())
        return await checked(conn, user, access, await STORE.row(conn, user.user_id, access.project_id))
