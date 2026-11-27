"""The plugins of a project: GET /v1/projects/{p}/plugins and .../plugins/{name}, POST .../plugins/{name}/enable and
/disable (``service.grants``), and the project's and its agent profiles' layers of a plugin's
config: GET and POST .../plugins/{name}/config, GET .../config/versions, POST .../config/rollback and GET
.../config/effective, each for the project's layer, or with ``agent`` for an agent profile's
(``service.configs``). GET .../runs/{id}/plugins says what a run ran with.

Every member of the project reads; someone without a grant gets the 404 of ``project_access``, a hub admin without
one a 403. Enabling, disabling and writing a layer need the admin role on the project: a writer or a reader gets 403.
A layer is written for a plugin the project enabled (409 otherwise), checked against the schema of the installed
version. The effective config is that of runs under ``agent``, the project's default profile when none is named,
that give no layer of their own: what a run that names no agent gets.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import profiles
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.plugins.models.configs import (
    EffectiveConfig,
    PluginConfigLayer,
    PluginConfigRollback,
    PluginConfigWrite,
    RunPlugin,
)
from evo_agents.hub.server.plugins.models.grants import PluginEnable, ProjectPlugin
from evo_agents.hub.server.plugins.models.store import PluginName
from evo_agents.hub.server.plugins.service import projects as checks
from evo_agents.hub.server.plugins.service.configs import (
    effective,
    layer_versions,
    read_layer,
    rollback_layer,
    run_plugins,
    write_layer,
)
from evo_agents.hub.server.plugins.service.grants import (
    check_admin,
    disable,
    enable,
    installed_row,
    list_project_plugins,
    project_plugin,
)
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser

AgentHandle = Annotated[
    str | None,
    Query(
        pattern=profiles.HANDLE_PATTERN,
        description="an agent profile's handle, @ left out: its layer, not the project's",
    ),
]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
CONFIG_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

router = APIRouter(prefix="/v1/projects", tags=["plugins"], responses={401: {"model": ErrorBody}})


# Enabling


def _secret(request: Request) -> str | None:
    """What signs the tickets of the plugins' pages (``service.pages``)."""
    return request.app.state.config.session_secret


@router.get("/{project}/plugins", response_model=list[ProjectPlugin], responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def plugins_of_project(request: Request, project: ProjectName, user: CurrentUser) -> list[ProjectPlugin]:
    """Every plugin installed on the hub, by name, with what it asks for and whether the project enabled it."""
    async with request.app.state.engine.begin() as conn:
        return await list_project_plugins(conn, await checks.reading(conn, user, project), _secret(request))


@router.get("/{project}/plugins/{name}", response_model=ProjectPlugin, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def plugin_of_project(request: Request, project: ProjectName, name: PluginName, user: CurrentUser):
    """One installed plugin: what it asks for, each permission with what it lets it do, before an admin grants it."""
    async with request.app.state.engine.begin() as conn:
        return await project_plugin(conn, await checks.reading(conn, user, project), name, _secret(request))


@router.post("/{project}/plugins/{name}/enable", response_model=ProjectPlugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def enable_plugin(
    request: Request, project: ProjectName, name: PluginName, body: PluginEnable, user: CurrentUser
) -> ProjectPlugin:
    """Enable the plugin for the project, granting exactly what its installed version asks for; or grant that again
    after an upgrade asked for more."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"enabling plugin {name}")
        await enable(conn, user, access, name, body.permissions, body.records_level)
        return await project_plugin(conn, access, name, _secret(request))


@router.post("/{project}/plugins/{name}/disable", response_model=ProjectPlugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def disable_plugin(request: Request, project: ProjectName, name: PluginName, user: CurrentUser) -> ProjectPlugin:
    """Disable the plugin for the project: its grants go, the versions of its config stay."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"disabling plugin {name}")
        await disable(conn, user, access, name)
        return await project_plugin(conn, access, name, _secret(request))


# Config


@router.get("/{project}/plugins/{name}/config", response_model=PluginConfigLayer, responses=CONFIG_REFUSALS)  # pyright: ignore[reportArgumentType]
async def config_layer(
    request: Request, project: ProjectName, name: PluginName, user: CurrentUser, agent: AgentHandle = None
) -> PluginConfigLayer:
    """The current version of the project's layer of the plugin's config, or of agent profile ``agent``'s."""
    async with request.app.state.engine.begin() as conn:
        access = await checks.reading(conn, user, project)
        plugin = await checks.enabled(conn, access, name)
        return await read_layer(conn, await checks.place(conn, access, plugin, agent))


@router.get(
    "/{project}/plugins/{name}/config/versions",
    response_model=list[PluginConfigLayer],
    responses=CONFIG_REFUSALS,  # pyright: ignore[reportArgumentType]
)
async def config_versions(
    request: Request, project: ProjectName, name: PluginName, user: CurrentUser, agent: AgentHandle = None
) -> list[PluginConfigLayer]:
    """Every version of the layer, the newest first."""
    async with request.app.state.engine.begin() as conn:
        access = await checks.reading(conn, user, project)
        plugin = await checks.enabled(conn, access, name)
        return await layer_versions(conn, await checks.place(conn, access, plugin, agent))


@router.post("/{project}/plugins/{name}/config", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def write_config(
    request: Request,
    project: ProjectName,
    name: PluginName,
    body: PluginConfigWrite,
    user: CurrentUser,
    agent: AgentHandle = None,
) -> PluginConfigLayer:
    """Write the layer's next version: the fields ``values`` sets, those ``unset`` clears, every other as it is."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"changing the config of plugin {name}")
        await installed_row(conn, access, name, lock=True)  # one write of the plugin's config at a time
        plugin = await checks.enabled(conn, access, name)
        place = await checks.place(conn, access, plugin, agent, live=True)
        return await write_layer(conn, user, place, plugin.manifest.get("config"), body, request.app.state.sealer)


@router.post("/{project}/plugins/{name}/config/rollback", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def rollback_config(
    request: Request,
    project: ProjectName,
    name: PluginName,
    body: PluginConfigRollback,
    user: CurrentUser,
    agent: AgentHandle = None,
) -> PluginConfigLayer:
    """Copy an older version of the layer as its next version."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_admin(access, f"rolling back the config of plugin {name}")
        await installed_row(conn, access, name, lock=True)
        plugin = await checks.enabled(conn, access, name)
        return await rollback_layer(conn, user, await checks.place(conn, access, plugin, agent, live=True), body)


@router.get("/{project}/plugins/{name}/config/effective", response_model=EffectiveConfig, responses=CONFIG_REFUSALS)  # pyright: ignore[reportArgumentType]
async def effective_config(
    request: Request, project: ProjectName, name: PluginName, user: CurrentUser, agent: AgentHandle = None
) -> EffectiveConfig:
    """The config the plugin gets in the project for runs under ``agent`` (the default profile without one) that give
    no layer of their own: each field with its value (none for a secret), the layer that set it, and the digest."""
    async with request.app.state.engine.begin() as conn:
        access = await checks.reading(conn, user, project)
        plugin = await checks.enabled(conn, access, name)
        profile_id, handle = await checks.profile(conn, access, agent)
        return await effective(conn, plugin, access.project_id, access.name, profile_id, handle)


# Runs


@router.get("/{project}/runs/{run_id}/plugins", response_model=list[RunPlugin], responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def plugins_of_run(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> list[RunPlugin]:
    """The plugins run ``run_id`` ran with, each with its version and the digest of its effective config, as a
    worker claimed it; empty for a run not claimed yet, or on a project with no plugin enabled."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        return await run_plugins(conn, run_id)
