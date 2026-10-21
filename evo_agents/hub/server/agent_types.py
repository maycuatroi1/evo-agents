"""The agent types of plugins in a project (``evo_agents.hub.task_runs``; ``docs/plugins.md``, section Agent types):
GET /v1/projects/{p}/agent-types, the agent types the plugins the project enabled contribute, each with how the
project enabled it; PUT .../agent-types/{plugin}/{name}, an admin of the project enables one or changes how; DELETE
.../agent-types/{plugin}/{name}, an admin turns it off.

Enabling a type the first time creates its agent profile in the project from the type's template: the handle, name,
description, instructions and permissions it names, the agent packs of the plugin it names attached at the version the
project runs (``agent_packs.pinned``), and its environment, when it names one, as the only one its runs may take. A
handle another agent holds, deleted ones included, or the name of a project refuses it (409), as does a plugin the
project did not enable or that is turned off. Enabling it again keeps that profile, which the project's admins edit as
any other, and changes the rest: the member whose name its runs go under (``run_as``, a writer or an admin of the
project), the workers of that member they may run on (none named: any the profile allows) and the cap of what its task
runs may cost in ``task_runs.BUDGET_HOURS`` (``daily_usd``). Turning it off keeps the row and the profile: the plugin's
tasks are refused until an admin enables it again. Every member of the project reads the list.

Each change is audited in the project: agent-type.enable, its target ``<project>/plugin:<plugin> agent-type:<name>``
then `` @<handle> run-as=<login> workers=<names> daily-usd=<cap>``, and agent-type.disable; the profile it creates
adds its agent.create.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, profiles, tables, task_runs
from evo_agents.hub.access import has_role
from evo_agents.hub.plugin_calls import enabled_in, enabled_plugins
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_packs import AgentPackPin, pinned
from evo_agents.hub.server.agent_profiles import AgentPermissions
from evo_agents.hub.server.chat_members import LOGIN_PATTERN
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import write_version
from evo_agents.hub.server.projects import PRINTABLE, ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.worker_models import WORKER_NAME
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.runtimes import CONTRIBUTION

log = logging.getLogger(__name__)

POINT = "agent_types"
NOTHING = "nothing changed"
WINDOW = func.make_interval(0, 0, 0, 0, task_runs.BUDGET_HOURS)

PluginName = Annotated[str, Path(pattern=PLUGIN_ID, description="the plugin's id")]
TypeName = Annotated[str, Path(pattern=f"^{CONTRIBUTION}$", description="the agent type, as the manifest names it")]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


class AgentTypeIn(BaseModel):
    """How a project runs an agent type: under whose name, on which of that member's workers, at what cost a day."""

    model_config = ConfigDict(extra="forbid")

    run_as: str = Field(
        pattern=LOGIN_PATTERN,
        description="the member whose name its runs go under: a writer or an admin of the project",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        default_factory=list,
        max_length=task_runs.MAX_WORKERS,
        description="the workers of run_as its runs may take, by name; none: any the profile allows",
    )
    daily_usd: float = Field(
        gt=0,
        le=task_runs.MAX_DAILY_USD,
        description=f"what its task runs may cost in {task_runs.BUDGET_HOURS} hours; a task past it is refused",
    )


class AgentType(BaseModel):
    """An agent type a plugin the project enabled contributes, and how the project enabled it (null: never)."""

    plugin: str
    name: str
    title: str | None
    description: str | None
    handle: str = Field(description="its profile's handle: the one the hub made, or the one its template names")
    environment: str | None = Field(description="<plugin>/<name>, the environment its runs take")
    packs: list[str] = Field(description="the agent packs of the plugin it attaches, <plugin>/<name>")
    enabled: bool = Field(description="enabled in the project and not turned off: the plugin may hand it tasks")
    run_as: str | None
    workers: list[str]
    daily_usd: float | None
    spent_usd: float = Field(description=f"what its task runs cost in the last {task_runs.BUDGET_HOURS} hours")
    enabled_by: str | None
    enabled_at: datetime | None
    disabled_at: datetime | None


def contributed(manifest: dict, name: str) -> dict | None:
    """The agent type ``name`` of ``manifest`` (as stored), or None."""
    return next((item for item in (manifest.get("contributes") or {}).get(POINT, ()) if item["name"] == name), None)


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} needs the admin role on project {access.name}; you hold {held}")


async def type_spend(conn: AsyncConnection, agent_type_id: int) -> float:
    """What the task runs of agent type ``agent_type_id`` queued in the last BUDGET_HOURS cost, as their usage says."""
    r = tables.runs
    query = select(r.c.usage).where(r.c.agent_type_id == agent_type_id, r.c.queued_at > func.now() - WINDOW)
    return round(sum(curator.run_cost(usage) for usage in (await conn.execute(query)).scalars()), 4)


def _rows(project_id: int):
    """The agent types project ``project_id`` enabled, with their profile's handle and their members' logins."""
    t, ap = tables.project_agent_types, tables.agent_profiles
    runner, enabler = tables.users.alias("runner"), tables.users.alias("enabler")
    return (
        select(
            t.c.id,
            t.c.plugin_id,
            t.c.name,
            t.c.profile_id,
            t.c.run_as.label("run_as_id"),
            runner.c.login.label("run_as"),
            t.c.workers,
            t.c.daily_usd,
            enabler.c.login.label("enabled_by"),
            t.c.enabled_at,
            t.c.disabled_at,
            ap.c.handle,
            ap.c.deleted_at.label("profile_deleted_at"),
        )
        .join_from(t, ap, ap.c.id == t.c.profile_id)
        .join(runner, runner.c.id == t.c.run_as)
        .join(enabler, enabler.c.id == t.c.enabled_by)
        .where(t.c.project_id == project_id)
    )


async def enabled_type(conn: AsyncConnection, project_id: int, plugin_id: int, name: str, *, lock: bool = False):
    """The row of ``_rows`` of agent type ``name`` of plugin ``plugin_id`` in project ``project_id``, or None."""
    t = tables.project_agent_types
    query = _rows(project_id).where(t.c.plugin_id == plugin_id, t.c.name == name)
    return (await conn.execute(query.with_for_update(of=t) if lock else query)).one_or_none()


def _view(plugin: str, item: dict, row, spent: float) -> AgentType:
    environment = item.get("environment")
    return AgentType(
        plugin=plugin,
        name=item["name"],
        title=item.get("title"),
        description=item.get("description"),
        handle=row.handle if row is not None else item["profile"]["handle"],
        environment=f"{plugin}/{environment}" if environment else None,
        packs=[f"{plugin}/{pack}" for pack in item.get("packs") or ()],
        enabled=row is not None and row.disabled_at is None and row.profile_deleted_at is None,
        run_as=row.run_as if row is not None else None,
        workers=list(row.workers or ()) if row is not None else [],
        daily_usd=row.daily_usd if row is not None else None,
        spent_usd=spent,
        enabled_by=row.enabled_by if row is not None else None,
        enabled_at=row.enabled_at if row is not None else None,
        disabled_at=row.disabled_at if row is not None else None,
    )


@router.get("/{project}/agent-types", response_model=list[AgentType], responses=READ_REFUSALS)
async def list_agent_types(request: Request, project: ProjectName, user: CurrentUser) -> list[AgentType]:
    """The agent types of the plugins the project enabled, by plugin, then in its manifest's order."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the agent types of project {project} needs a grant on it")
        rows = {(row.plugin_id, row.name): row for row in await conn.execute(_rows(access.project_id))}
        found = []
        for plugin in await enabled_plugins(conn, access.project_id):
            for item in (plugin.manifest.get("contributes") or {}).get(POINT, ()):
                row = rows.get((plugin.id, item["name"]))
                spent = await type_spend(conn, row.id) if row is not None else 0.0
                found.append(_view(plugin.name, item, row, spent))
        return found


# Enabling


async def _contribution(conn: AsyncConnection, access: ProjectAccess, plugin: str, name: str):
    """(the row of ``plugin_calls.enabled_query`` of ``plugin`` in the project, its agent type ``name``)."""
    enabled = await enabled_in(conn, plugin, access.project_id)
    if enabled is None:
        raise HTTPException(
            409,
            f"plugin {plugin} is not enabled in project {access.name}, or it is turned off: enable it with POST "
            f"/v1/projects/{access.name}/plugins/{plugin}/enable first; {NOTHING}",
        )
    item = contributed(enabled.manifest, name)
    if item is None:
        raise HTTPException(
            404,
            f"plugin {plugin} {enabled.installed_version} contributes no agent type {name}: see GET "
            f"/v1/projects/{access.name}/agent-types",
        )
    return enabled, item


async def _member(conn: AsyncConnection, access: ProjectAccess, login: str) -> int:
    """The id of ``login``, a writer or an admin of the project; 422 otherwise."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.id, g.c.role)
        .select_from(u)
        .outerjoin(g, (g.c.user_id == u.c.id) & (g.c.project_id == access.project_id))
        .where(func.lower(u.c.login) == login.lower())
    )
    row = (await conn.execute(query)).first()
    if row is None or not has_role(row.role, "writer"):
        held = "is no member of the hub" if row is None else f"holds {row.role or 'no grant'} there"
        raise HTTPException(
            422, f"the runs of an agent type go under a writer of project {access.name}, and {login} {held}; {NOTHING}"
        )
    return row.id


async def _workers(conn: AsyncConnection, user_id: int, login: str, names: list[str]) -> list[str]:
    """``names``, each once, lowered: workers of ``user_id`` that are not revoked; 422 for one that is not."""
    wanted = list(dict.fromkeys(name.lower() for name in names))
    w = tables.workers
    query = select(func.lower(w.c.name)).where(w.c.owner_id == user_id, w.c.revoked_at.is_(None))
    held = set((await conn.execute(query)).scalars())
    missing = [name for name in wanted if name not in held]
    if missing:
        raise HTTPException(
            422,
            f"{login} has no worker {', '.join(missing)} that is not revoked: name workers of {login}, or none for "
            f"any; {NOTHING}",
        )
    return wanted


def _permissions(plugin: str, item: dict) -> dict:
    """The permissions of the profile of agent type ``item`` of ``plugin``, as stored: its template's, with its
    environment the only one its runs take; 422 for a template a profile cannot have."""
    try:
        stored = AgentPermissions.model_validate(item["profile"].get("permissions") or {}).stored()
    except ValidationError as exc:
        said = "; ".join(str(error["msg"]) for error in exc.errors())
        raise HTTPException(
            422, f"agent type {item['name']} of plugin {plugin} has permissions no profile takes: {said}; {NOTHING}"
        ) from None
    if item.get("environment"):
        stored["environment"] = {"mode": profiles.ALLOWLIST, "allow": [f"{plugin}/{item['environment']}"]}
    return stored


async def _create_profile(conn: AsyncConnection, user, access: ProjectAccess, plugin: str, item: dict, hub: str) -> int:
    """Create the profile of agent type ``item`` of ``plugin`` in the project from its template, at version 1."""
    template, ap, p = item["profile"], tables.agent_profiles, tables.projects
    handle = template["handle"]
    if (await conn.execute(select(p.c.id).where(p.c.name == handle))).first() is not None:
        raise HTTPException(409, f"@{handle}, the handle agent type {item['name']} names, is a project's: {NOTHING}")
    name = (template.get("name") or item.get("title") or item["name"])[: profiles.MAX_NAME_CHARS]
    if not re.fullmatch(PRINTABLE, name):
        name = item["name"]
    pins = [AgentPackPin(plugin=plugin, name=pack) for pack in item.get("packs") or ()]
    values = {
        "handle": handle,
        "name": name,
        "description": template.get("description") or "",
        "instructions": template.get("instructions") or "",
        "color": profiles.default_color(handle),
        "avatar_sha256": None,
        "is_default": False,
        "permissions": _permissions(plugin, item),
        "packs": await pinned(conn, access.project_id, access.name, pins, hub),
    }
    statement = (
        pg_insert(ap)
        .values(project_id=access.project_id, created_by=user.user_id, **values)
        .on_conflict_do_nothing(index_elements=[ap.c.handle])
        .returning(ap.c.id)
    )
    profile_id = (await conn.execute(statement)).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(
            409,
            f"@{handle}, the handle agent type {item['name']} of plugin {plugin} names, is taken by another agent, "
            f"deleted ones included: {NOTHING}",
        )
    await write_version(conn, profile_id, 1, values, user.user_id)
    await _record(conn, user, access, audit.AGENT_CREATE, f"{access.name}/@{handle} v1")
    return profile_id


async def _record(conn: AsyncConnection, user, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


def _target(access: ProjectAccess, plugin: str, name: str) -> str:
    return f"{access.name}/plugin:{plugin} agent-type:{name}"


@router.put("/{project}/agent-types/{plugin}/{name}", response_model=AgentType, responses=WRITE_REFUSALS)
async def enable_agent_type(
    request: Request, project: ProjectName, plugin: PluginName, name: TypeName, body: AgentTypeIn, user: CurrentUser
) -> AgentType:
    """Enable agent type ``name`` of ``plugin`` in the project, creating its profile the first time, or change how."""
    t = tables.project_agent_types
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "enabling an agent type")
        enabled, item = await _contribution(conn, access, plugin, name)
        run_as = await _member(conn, access, body.run_as)
        workers = await _workers(conn, run_as, body.run_as, body.workers)
        row = await enabled_type(conn, access.project_id, enabled.id, name, lock=True)
        if row is not None and row.profile_deleted_at is not None:
            raise HTTPException(
                409,
                f"@{row.handle}, the profile of agent type {name}, was deleted, and a handle is never taken again: "
                f"{NOTHING}",
            )
        hub = request.app.state.hub_version
        profile_id = row.profile_id if row is not None else await _create_profile(conn, user, access, plugin, item, hub)
        values = {"profile_id": profile_id, "run_as": run_as, "workers": workers, "daily_usd": body.daily_usd}
        values |= {"enabled_by": user.user_id, "enabled_at": func.now(), "disabled_at": None}
        statement = pg_insert(t).values(project_id=access.project_id, plugin_id=enabled.id, name=name, **values)
        await conn.execute(statement.on_conflict_do_update(constraint="project_agent_types_name_key", set_=values))
        row = await enabled_type(conn, access.project_id, enabled.id, name)
        said = f" @{row.handle} run-as={row.run_as} workers={','.join(workers) or '-'} daily-usd={body.daily_usd}"
        await _record(conn, user, access, audit.AGENT_TYPE_ENABLE, _target(access, plugin, name) + said)
        view = _view(plugin, item, row, await type_spend(conn, row.id))
    log.info("agent type enabled", extra={"project": project, "plugin": plugin, "agent_type": name})
    return view


@router.delete("/{project}/agent-types/{plugin}/{name}", response_model=AgentType, responses=WRITE_REFUSALS)
async def disable_agent_type(
    request: Request, project: ProjectName, plugin: PluginName, name: TypeName, user: CurrentUser
) -> AgentType:
    """Turn agent type ``name`` of ``plugin`` off in the project: the plugin's tasks are refused; its profile stays."""
    t = tables.project_agent_types
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "turning off an agent type")
        enabled, item = await _contribution(conn, access, plugin, name)
        row = await enabled_type(conn, access.project_id, enabled.id, name, lock=True)
        if row is None or row.disabled_at is not None:
            raise HTTPException(409, f"agent type {name} of plugin {plugin} is not enabled in project {project}")
        await conn.execute(update(t).where(t.c.id == row.id).values(disabled_at=func.now()))
        await _record(conn, user, access, audit.AGENT_TYPE_DISABLE, _target(access, plugin, name))
        row = await enabled_type(conn, access.project_id, enabled.id, name)
        view = _view(plugin, item, row, await type_spend(conn, row.id))
    log.info("agent type turned off", extra={"project": project, "plugin": plugin, "agent_type": name})
    return view
