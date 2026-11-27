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
from evo_agents.hub.server.plugins.service.actions import backend_of, install, instance_place, publish, remove
from evo_agents.hub.server.plugins.service.configs import layer_versions, read_layer, rollback_layer, write_layer
from evo_agents.hub.server.plugins.service.store import detail, list_plugins
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
    async with request.app.state.engine.begin() as conn:
        return await list_plugins(conn, request.app.state.hub_version)


@router.get("/{name}", response_model=PluginDetail, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def show_plugin(request: Request, name: PluginName) -> PluginDetail:
    """One plugin with every version, newest first."""
    async with request.app.state.engine.begin() as conn:
        return await detail(conn, name, request.app.state.hub_version)


@router.post("/{name}/versions", response_model=PluginPublished, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def publish_version(request: Request, name: PluginName, body: PluginPublishIn, user: AdminUser):
    """Publish a signed bundle the hub holds as a version of plugin ``name``, after checking it whole."""
    return await publish(request.app.state, blob_store(request), user, name, body)


@router.post("/{name}/install", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def install_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Install a version of plugin ``name``: the one asked for, or the newest that runs on this hub."""
    return await install(request.app.state, lambda: blob_store(request), user, name, body.version, upgrade=False)


@router.post("/{name}/upgrade", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def upgrade_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Move the installed version of plugin ``name`` to a newer one: the one asked for, or the newest that runs."""
    return await install(request.app.state, lambda: blob_store(request), user, name, body.version, upgrade=True)


@router.post("/{name}/remove", response_model=Plugin, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def remove_plugin(request: Request, name: PluginName, user: AdminUser) -> Plugin:
    """Remove the installed version of plugin ``name``; every version stays in the store."""
    async with request.app.state.engine.begin() as conn:
        return await remove(conn, user, name, request.app.state.hub_version)


# The hub's own layer of a plugin's config


@router.get("/{name}/backend", response_model=BackendStatus, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def plugin_backend(request: Request, name: PluginName) -> BackendStatus:
    """The state of the plugin's backend as the plugin host last reported it: running, restarting, or in backoff with
    its last failure, for a hub admin to read. A plugin the host has not reported on reads as such."""
    async with request.app.state.engine.begin() as conn:
        return await backend_of(conn, name)


@router.get("/{name}/config", response_model=PluginConfigLayer, responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def instance_config(request: Request, name: PluginName) -> PluginConfigLayer:
    """The current version of the hub's own layer of the plugin's config."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await instance_place(conn, name)
        return await read_layer(conn, place)


@router.get("/{name}/config/versions", response_model=list[PluginConfigLayer], responses=READ_REFUSALS)  # pyright: ignore[reportArgumentType]
async def instance_config_versions(request: Request, name: PluginName) -> list[PluginConfigLayer]:
    """Every version of the hub's own layer, the newest first."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await instance_place(conn, name)
        return await layer_versions(conn, place)


@router.post("/{name}/config", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def write_instance_config(
    request: Request, name: PluginName, body: PluginConfigWrite, user: AdminUser
) -> PluginConfigLayer:
    """Write the next version of the hub's own layer: the fields ``values`` sets, those ``unset`` clears."""
    async with request.app.state.engine.begin() as conn:
        place, schema = await instance_place(conn, name, lock=True)
        return await write_layer(conn, user, place, schema, body, request.app.state.sealer)


@router.post("/{name}/config/rollback", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def rollback_instance_config(
    request: Request, name: PluginName, body: PluginConfigRollback, user: AdminUser
) -> PluginConfigLayer:
    """Copy an older version of the hub's own layer as its next version."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await instance_place(conn, name, lock=True)
        return await rollback_layer(conn, user, place, body)
