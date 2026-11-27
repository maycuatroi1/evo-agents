"""The agent types of plugins in a project (``evo_agents.hub.task_runs``; ``docs/plugins.md``, section Agent types):
GET /v1/projects/{p}/agent-types, the agent types the plugins the project enabled contribute, each with how the
project enabled it; PUT .../agent-types/{plugin}/{name}, an admin of the project enables one or changes how; DELETE
.../agent-types/{plugin}/{name}, an admin turns it off.

Enabling a type the first time creates its agent profile in the project from the type's template: the handle, name,
description, instructions and permissions it names, the agent packs of the plugin it names attached at the version the
project runs (``agent_packs.pinned``), and its environment, when it names one, as the only one its runs may take; and
the agent's own pool, ``agent-<handle>``, where its runs go (``profile_store.create_pool``). A
handle is unique on the hub and a core plugin's type is enabled in many projects, so the profile takes the handle the
admin names (``handle``), else the template's, else ``<template's>-<project>`` when another agent, deleted ones
included, or a project holds the template's (``_handle``); none of them free refuses it (409), as does a plugin the
project did not enable or that is turned off. Enabling it again keeps that profile, which the project's admins edit as
any other, and changes the rest: the member whose name its runs go under (``run_as``, a writer or an admin of the
project) and the cap of what its task runs may cost in ``task_runs.BUDGET_HOURS`` (``daily_usd``). Turning it off keeps the row and the profile: the plugin's
tasks are refused until an admin enables it again. Every member of the project reads the list.

Each change is audited in the project: agent-type.enable, its target ``<project>/plugin:<plugin> agent-type:<name>``
then `` @<handle> run-as=<login> daily-usd=<cap>``, and agent-type.disable; the profile it creates
adds its agent.create.
"""

from __future__ import annotations

import re

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, profiles, task_runs
from evo_agents.hub.plugin_calls import enabled_in, enabled_plugins
from evo_agents.hub.server import audit
from evo_agents.hub.server.agents.models.agent_types import MAX_HANDLE, NOTHING, POINT, AgentType, AgentTypeIn
from evo_agents.hub.server.agents.models.packs import AgentPackPin
from evo_agents.hub.server.agents.models.profiles import AgentPermissions
from evo_agents.hub.server.agents.repository import agent_types as repository
from evo_agents.hub.server.agents.repository.profiles import insert_profile
from evo_agents.hub.server.agents.service.packs import pinned
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.profile_store import create_pool, write_version
from evo_agents.hub.server.projects import PRINTABLE, ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds


def contributed(manifest: dict, name: str) -> dict | None:
    """The agent type ``name`` of ``manifest`` (as stored), or None."""
    return next((item for item in (manifest.get("contributes") or {}).get(POINT, ()) if item["name"] == name), None)


def _admin(access: ProjectAccess, doing: str) -> None:
    if not holds(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(f"{doing} needs the admin role on project {access.name}; you hold {held}")


async def type_spend(conn: AsyncConnection, agent_type_id: int) -> float:
    """What the task runs of agent type ``agent_type_id`` queued in the last BUDGET_HOURS cost, as their usage says."""
    return round(sum(curator.run_cost(usage) for usage in await repository.recent_usages(conn, agent_type_id)), 4)


async def type_hold(conn: AsyncConnection, agent_type_id: int, daily_usd: float) -> tuple[float, float, float]:
    """(spent, held, floor) of agent type ``agent_type_id`` whose cap is ``daily_usd``: what its ended runs queued in
    the last BUDGET_HOURS cost, what each run not ended holds (the floor of the type, or the cap of the run when that
    is less, and never less than the run spent already), and the floor in effect (``task_runs.run_floor``)."""
    floor = task_runs.run_floor(await repository.set_floor(conn, agent_type_id), daily_usd)
    spent = held = 0.0
    for usage, budget, over in await repository.held_runs(conn, agent_type_id):
        cost = curator.run_cost(usage)
        if over:
            spent += cost
            continue
        cap = (budget or {}).get("max_usd") if isinstance(budget, dict) else None
        cap = cap if isinstance(cap, int | float) and not isinstance(cap, bool) else floor
        held += max(min(floor, cap), cost)
    return round(spent, 4), round(held, 4), floor


async def type_left(conn: AsyncConnection, row, *, waits: str = "the task") -> tuple[float, str | None]:
    """(what is left of the cap of agent type ``row`` for the next run, why that run may not be handed or None): the
    one rule of every dispatch (``task_runs.left_problem``). The caller holds the row's lock."""
    spent, held, floor = await type_hold(conn, row.id, row.daily_usd)
    problem = task_runs.left_problem(spent, held, floor, row.daily_usd, row.name, waits=waits)
    return task_runs.run_left(spent, held, row.daily_usd), problem


async def enabled_type(conn: AsyncConnection, project_id: int, plugin_id: int, name: str, *, lock: bool = False):
    """The row of ``repository.rows`` of agent type ``name`` of plugin ``plugin_id`` in project ``project_id``, or
    None."""
    return await repository.enabled_type(conn, project_id, plugin_id, name, lock=lock)


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
        daily_usd=row.daily_usd if row is not None else None,
        min_run_usd=row.min_run_usd if row is not None else None,
        effective_min_run_usd=(
            task_runs.run_floor(row.min_run_usd, row.daily_usd)
            if row is not None and row.daily_usd is not None
            else None
        ),
        spent_usd=spent,
        enabled_by=row.enabled_by if row is not None else None,
        enabled_at=row.enabled_at if row is not None else None,
        disabled_at=row.disabled_at if row is not None else None,
    )


async def list_types(engine: AsyncEngine, user: Principal, project: str) -> list[AgentType]:
    """The agent types of the plugins the project enabled, by plugin, then in its manifest's order."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise Forbidden(f"reading the agent types of project {project} needs a grant on it")
        rows = {(row.plugin_id, row.name): row for row in await repository.project_types(conn, access.project_id)}
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
        raise Conflict(
            f"plugin {plugin} is not enabled in project {access.name}, or it is turned off: enable it with POST "
            f"/v1/projects/{access.name}/plugins/{plugin}/enable first; {NOTHING}"
        )
    item = contributed(enabled.manifest, name)
    if item is None:
        raise NotFound(
            f"plugin {plugin} {enabled.installed_version} contributes no agent type {name}: see GET "
            f"/v1/projects/{access.name}/agent-types"
        )
    return enabled, item


async def _member(conn: AsyncConnection, access: ProjectAccess, login: str) -> int:
    """The id of ``login``, a writer or an admin of the project; 422 otherwise."""
    row = await repository.member_role(conn, access.project_id, login)
    if row is None or not holds(row.role, "writer"):
        held = "is no member of the hub" if row is None else f"holds {row.role or 'no grant'} there"
        raise Unprocessable(
            f"the runs of an agent type go under a writer of project {access.name}, and {login} {held}; {NOTHING}"
        )
    return row.id


def _permissions(plugin: str, item: dict) -> dict:
    """The permissions of the profile of agent type ``item`` of ``plugin``, as stored: its template's, with its
    environment the only one its runs take; 422 for a template a profile cannot have."""
    try:
        stored = AgentPermissions.model_validate(item["profile"].get("permissions") or {}).stored()
    except ValidationError as exc:
        said = "; ".join(str(error["msg"]) for error in exc.errors())
        raise Unprocessable(
            f"agent type {item['name']} of plugin {plugin} has permissions no profile takes: {said}; {NOTHING}"
        ) from None
    if item.get("environment"):
        stored["environment"] = {"mode": profiles.ALLOWLIST, "allow": [f"{plugin}/{item['environment']}"]}
    return stored


async def _handle(conn: AsyncConnection, access: ProjectAccess, plugin: str, item: dict, wanted: str | None) -> str:
    """The handle of the profile agent type ``item`` of ``plugin`` gets in the project: ``wanted`` when the admin names
    one; else its template's, or ``<template's>-<project>`` when another agent of the hub (deleted ones included) or a
    project holds the template's, since a handle is unique on the hub and a core plugin's type is enabled in many
    projects. 409 when none of them is free."""
    own = item["profile"]["handle"]
    candidates = [wanted] if wanted else [own, f"{own}-{access.name}"[:MAX_HANDLE].rstrip("-")]
    held = await repository.taken_handles(conn, candidates)
    free = next((handle for handle in candidates if handle not in held), None)
    if free is not None:
        return free
    taken = " and ".join(f"@{handle}" for handle in candidates)
    raise Conflict(
        f"{taken}, the handle{'s' if len(candidates) > 1 else ''} agent type {item['name']} of plugin {plugin} would "
        f"take, {'are' if len(candidates) > 1 else 'is'} another agent's, deleted ones included, or a project's: name "
        f"a free one in handle; {NOTHING}"
    )


async def _create_profile(
    conn: AsyncConnection, user, access: ProjectAccess, plugin: str, item: dict, hub: str, wanted: str | None
) -> int:
    """Create the profile of agent type ``item`` of ``plugin`` in the project from its template, at version 1, under
    the handle ``_handle`` picks."""
    template = item["profile"]
    handle = await _handle(conn, access, plugin, item, wanted)
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
    profile_id = await insert_profile(conn, access.project_id, user.user_id, values)
    if profile_id is None:
        raise Conflict(
            f"@{handle}, the handle agent type {item['name']} of plugin {plugin} would take, was taken by another "
            f"agent meanwhile: {NOTHING}"
        )
    await write_version(conn, profile_id, 1, values, user.user_id)
    await create_pool(conn, profile_id, handle, access.project_id, user.user_id)  # its runs go to its own pool
    await Auditor.of(conn, user, access).record(audit.AGENT_CREATE, f"{access.name}/@{handle} v1")
    return profile_id


def _target(access: ProjectAccess, plugin: str, name: str) -> str:
    return f"{access.name}/plugin:{plugin} agent-type:{name}"


def _kept_profile(row, body: AgentTypeIn, plugin: str, name: str, project: str) -> None:
    """409 when the profile of agent type ``row`` was deleted, or ``body`` names another handle than its."""
    if row is not None and row.profile_deleted_at is not None:
        raise Conflict(
            f"@{row.handle}, the profile of agent type {name}, was deleted, and a handle is never taken again: "
            f"{NOTHING}"
        )
    if row is not None and body.handle is not None and body.handle != row.handle:
        raise Conflict(
            f"agent type {name} of plugin {plugin} has its profile @{row.handle} in project {project}, and a "
            f"handle never changes: {NOTHING}"
        )


async def enable(
    engine: AsyncEngine, request: Request, user: Principal, project: str, plugin: str, name: str, body: AgentTypeIn
) -> AgentType:
    """Enable agent type ``name`` of ``plugin`` in the project, creating its profile the first time, or change how."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        _admin(access, "enabling an agent type")
        if body.min_run_usd is not None and body.min_run_usd > body.daily_usd:
            raise Unprocessable(
                f"min_run_usd {body.min_run_usd} is above daily_usd {body.daily_usd}: a run could never be handed"
            )
        enabled, item = await _contribution(conn, access, plugin, name)
        run_as = await _member(conn, access, body.run_as)
        row = await repository.enabled_type(conn, access.project_id, enabled.id, name, lock=True)
        _kept_profile(row, body, plugin, name, project)
        hub = request.app.state.hub_version
        profile_id = (
            row.profile_id
            if row is not None
            else await _create_profile(conn, user, access, plugin, item, hub, body.handle)
        )
        values = {"profile_id": profile_id, "run_as": run_as, "daily_usd": body.daily_usd}
        if body.min_run_usd is not None:  # none: keep the floor already set
            values["min_run_usd"] = body.min_run_usd
        values |= {"enabled_by": user.user_id, "disabled_at": None}
        await repository.put_type(conn, access.project_id, enabled.id, name, values)
        row = await repository.enabled_type(conn, access.project_id, enabled.id, name)
        assert row is not None  # written just above, in this transaction
        said = f" @{row.handle} run-as={row.run_as} daily-usd={body.daily_usd}"
        if body.min_run_usd is not None:
            said += f" min-run-usd={body.min_run_usd}"
        await Auditor.of(conn, user, access).record(audit.AGENT_TYPE_ENABLE, _target(access, plugin, name) + said)
        return _view(plugin, item, row, await type_spend(conn, row.id))


async def disable(engine: AsyncEngine, user: Principal, project: str, plugin: str, name: str) -> AgentType:
    """Turn agent type ``name`` of ``plugin`` off in the project: the plugin's tasks are refused; its profile stays."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        _admin(access, "turning off an agent type")
        enabled, item = await _contribution(conn, access, plugin, name)
        row = await repository.enabled_type(conn, access.project_id, enabled.id, name, lock=True)
        if row is None or row.disabled_at is not None:
            raise Conflict(f"agent type {name} of plugin {plugin} is not enabled in project {project}")
        await repository.turn_off(conn, row.id)
        await Auditor.of(conn, user, access).record(audit.AGENT_TYPE_DISABLE, _target(access, plugin, name))
        row = await repository.enabled_type(conn, access.project_id, enabled.id, name)
        assert row is not None  # locked above, in this transaction
        return _view(plugin, item, row, await type_spend(conn, row.id))
