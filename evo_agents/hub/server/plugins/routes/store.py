"""The routes of the hub's store of plugins, for hub admins only (``service.store`` holds the design, ``models.store``
the models and ``repository.store`` the queries).

- GET /v1/plugins: every plugin the store holds, its state (available, installed, disabled), installed and latest
  version, and the newest version above the installed one that runs on this hub;
- GET /v1/plugins/{name}: one plugin with every version, newest first, and the manifest of the installed one (else of
  the latest);
- POST /v1/plugins/{name}/versions: publish the bundle committed to the blob store with its signature;
- POST /v1/plugins/{name}/install, /upgrade and /remove: install a version (the newest that runs by default), move
  the installed one to a newer version, remove it;
- GET and POST /v1/plugins/{name}/config, GET .../config/versions and POST .../config/rollback: the hub's own layer
  (instance) of the config of an installed plugin, below every project's (``service.configs``);

The publishers whose signatures publishing and installing accept are the operator's and those hub admins trusted
(``routes.publishers``, GET|POST /v1/plugin-publishers).

Writes are audited as plugin.publish, plugin.install, plugin.upgrade and plugin.remove, naming
``plugin:<name>@<version>``, and plugin.config. ``evo-agents hub plugin publish|list|show|install|upgrade|remove``
and ``evo-agents hub plugin config|rollback --instance`` are the client.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.configs import PluginConfigLayer, PluginConfigRollback, PluginConfigWrite
from evo_agents.hub.server.plugins.models.host import BackendStatus
from evo_agents.hub.server.plugins.models.store import (
    Plugin,
    PluginDetail,
    PluginInstallIn,
    PluginName,
    PluginPublished,
    PluginPublishIn,
)
from evo_agents.hub.server.plugins.service import actions
from evo_agents.hub.server.security import AdminUser, admin

READ_REFUSALS = {404: {"model": ErrorBody}}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422, 503)}

router = APIRouter(
    prefix="/v1/plugins",
    tags=["plugins"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


@router.get("", response_model=list[Plugin])
async def plugins(request: Request) -> list[Plugin]:
    """Every plugin of the hub's store, by name."""
    return await actions.plugins(request.app.state.engine, request.app.state.hub_version)


@router.get("/{name}", response_model=PluginDetail, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def show_plugin(request: Request, name: PluginName) -> PluginDetail:
    """One plugin with every version, newest first."""
    return await actions.plugin_detail(request.app.state.engine, name, request.app.state.hub_version)


@router.post("/{name}/versions", response_model=PluginPublished, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def publish_version(request: Request, name: PluginName, body: PluginPublishIn, user: AdminUser):
    """Publish a signed bundle the hub holds as a version of plugin ``name``, after checking it whole."""
    return await actions.publish(request.app.state, blob_store(request), user, name, body)


@router.post("/{name}/install", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def install_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Install a version of plugin ``name``: the one asked for, or the newest that runs on this hub."""
    state = request.app.state
    return await actions.install(state, lambda: blob_store(request), user, name, body.version, upgrade=False)


@router.post("/{name}/upgrade", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def upgrade_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Move the installed version of plugin ``name`` to a newer one: the one asked for, or the newest that runs."""
    state = request.app.state
    return await actions.install(state, lambda: blob_store(request), user, name, body.version, upgrade=True)


@router.post("/{name}/remove", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def remove_plugin(request: Request, name: PluginName, user: AdminUser) -> Plugin:
    """Remove the installed version of plugin ``name``; every version stays in the store."""
    return await actions.remove(request.app.state.engine, user, name, request.app.state.hub_version)


# The hub's own layer of a plugin's config


@router.get("/{name}/backend", response_model=BackendStatus, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def plugin_backend(request: Request, name: PluginName) -> BackendStatus:
    """The state of the plugin's backend as the plugin host last reported it: running, restarting, or in backoff with
    its last failure, for a hub admin to read. A plugin the host has not reported on reads as such."""
    return await actions.backend_of(request.app.state.engine, name)


@router.get("/{name}/config", response_model=PluginConfigLayer, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def instance_config(request: Request, name: PluginName) -> PluginConfigLayer:
    """The current version of the hub's own layer of the plugin's config."""
    return await actions.instance_config(request.app.state.engine, name)


@router.get("/{name}/config/versions", response_model=list[PluginConfigLayer], responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def instance_config_versions(request: Request, name: PluginName) -> list[PluginConfigLayer]:
    """Every version of the hub's own layer, the newest first."""
    return await actions.instance_config_versions(request.app.state.engine, name)


@router.post("/{name}/config", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def write_instance_config(
    request: Request, name: PluginName, body: PluginConfigWrite, user: AdminUser
) -> PluginConfigLayer:
    """Write the next version of the hub's own layer: the fields ``values`` sets, those ``unset`` clears."""
    return await actions.write_instance_config(request.app.state.engine, user, name, body, request.app.state.sealer)


@router.post("/{name}/config/rollback", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def rollback_instance_config(
    request: Request, name: PluginName, body: PluginConfigRollback, user: AdminUser
) -> PluginConfigLayer:
    """Copy an older version of the hub's own layer as its next version."""
    return await actions.rollback_instance_config(request.app.state.engine, user, name, body)
