"""The routes of the hub's store of plugins, for hub admins only (``evo_agents.hub.server.plugin_store`` holds the
design, the models and the queries).

- GET /v1/plugins: every plugin the store holds, its state (available, installed, disabled), installed and latest
  version, and the newest version above the installed one that runs on this hub;
- GET /v1/plugins/{name}: one plugin with every version, newest first, and the manifest of the installed one (else of
  the latest);
- POST /v1/plugins/{name}/versions: publish the bundle committed to the blob store with its signature;
- POST /v1/plugins/{name}/install, /upgrade and /remove: install a version (the newest that runs by default), move
  the installed one to a newer version, remove it;
- GET and POST /v1/plugins/{name}/config, GET .../config/versions and POST .../config/rollback: the hub's own layer
  (instance) of the config of an installed plugin, below every project's (``evo_agents.hub.server.plugin_configs``);

The publishers whose signatures publishing and installing accept are the operator's and those hub admins trusted
(``evo_agents.hub.server.plugin_publishers``, GET|POST /v1/plugin-publishers).

Writes are audited as plugin.publish, plugin.install, plugin.upgrade and plugin.remove, naming
``plugin:<name>@<version>``, and plugin.config. ``evo-agents hub plugin publish|list|show|install|upgrade|remove``
and ``evo-agents hub plugin config|rollback --instance`` are the client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market_licenses import licence_problem
from evo_agents.hub.server.plugin_configs import (
    Place,
    PluginConfigLayer,
    PluginConfigRollback,
    PluginConfigWrite,
    layer_versions,
    read_layer,
    rollback_layer,
    write_layer,
)
from evo_agents.hub.server.plugin_host import BackendStatus, backend_status
from evo_agents.hub.server.plugin_store import (
    FROM_IMAGE,
    PLUGIN_NAME,
    Plugin,
    PluginDetail,
    PluginInstallIn,
    PluginPublished,
    PluginPublishIn,
    check_bundle,
    check_placeable,
    check_state,
    committed,
    detail,
    fetch_bundle,
    find,
    from_image,
    held_version,
    list_plugins,
    not_found,
    pick_version,
    plugin,
    record_version,
    set_installed,
    set_removed,
    trusted_publishers,
    version_model,
    version_rows,
)
from evo_agents.hub.server.security import AdminUser, admin

log = logging.getLogger(__name__)

NOTHING_PUBLISHED = "nothing was published"
NOTHING_INSTALLED = "nothing was installed"
PluginName = Annotated[str, Path(pattern=PLUGIN_NAME, description="the plugin's id, as its manifest gives it")]
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


@router.get("/{name}", response_model=PluginDetail, responses=READ_REFUSALS)
async def show_plugin(request: Request, name: PluginName) -> PluginDetail:
    """One plugin with every version, newest first."""
    async with request.app.state.engine.begin() as conn:
        return await detail(conn, name, request.app.state.hub_version)


@router.post("/{name}/versions", response_model=PluginPublished, responses=WRITE_REFUSALS)
async def publish_version(request: Request, name: PluginName, body: PluginPublishIn, user: AdminUser):
    """Publish a signed bundle the hub holds as a version of plugin ``name``, after checking it whole."""
    state = request.app.state
    store = blob_store(request)
    async with state.engine.begin() as conn:
        await committed(conn, body.sha256, body.size)
        held = await held_version(conn, name, body.sha256)
        if held is not None:
            return PluginPublished(name=name, created=False, version=version_model(held, state.hub_version))
        trusted = await trusted_publishers(conn, state.config)
    data = await fetch_bundle(store, body.sha256, body.size)
    sigstore, version = state.plugin_sigstore, state.hub_version
    checked = await asyncio.to_thread(
        check_bundle, data, body.signature, state.config, sigstore, version, NOTHING_PUBLISHED, trusted
    )
    if checked.manifest.id != name:
        raise HTTPException(
            422, f"the bundle is of plugin {checked.manifest.id}, not {name}: publish it as {checked.manifest.id}"
        )
    async with state.engine.begin() as conn:
        await committed(conn, body.sha256, body.size)
        created = await record_version(conn, user, body, checked)
        row = await find(conn, name)
        written = next(v for v in await version_rows(conn, row.id) if v.version == checked.manifest.version)
    log.info(
        "plugin published" if created else "plugin unchanged",
        extra={"plugin": name, "version": written.version, "signed_by": written.signed_by},
    )
    return PluginPublished(name=name, created=created, version=version_model(written, version))


async def _install(request: Request, user, name: str, wanted: str | None, *, upgrade: bool) -> Plugin:
    state = request.app.state
    async with state.engine.begin() as conn:
        row = await find(conn, name)
        rows = [] if row is None else await version_rows(conn, row.id)
        if not rows:
            raise not_found(name)
        if from_image(rows):
            raise HTTPException(409, FROM_IMAGE.format(name=name))
        check_state(row, upgrade=upgrade, target=wanted)
        problem = await licence_problem(conn, name)
        if problem is not None:
            raise HTTPException(409, problem)
        target = pick_version(name, rows, wanted, row.installed_version if upgrade else None, state.hub_version)
        check_placeable(row, target)
        trusted = await trusted_publishers(conn, state.config)
    data = await fetch_bundle(blob_store(request), target.sha256, target.size)
    await asyncio.to_thread(
        check_bundle,
        data,
        target.signature,
        state.config,
        state.plugin_sigstore,
        state.hub_version,
        NOTHING_INSTALLED,
        trusted,
    )
    async with state.engine.begin() as conn:
        row = await find(conn, name, lock=True)
        check_state(row, upgrade=upgrade, target=target.version)  # as it stands now, after the checks
        await set_installed(conn, user, row, target.version, upgrade=upgrade)
        result = await plugin(conn, name, state.hub_version)
    log.info("plugin upgraded" if upgrade else "plugin installed", extra={"plugin": name, "version": target.version})
    return result


@router.post("/{name}/install", response_model=Plugin, responses=WRITE_REFUSALS)
async def install_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Install a version of plugin ``name``: the one asked for, or the newest that runs on this hub."""
    return await _install(request, user, name, body.version, upgrade=False)


@router.post("/{name}/upgrade", response_model=Plugin, responses=WRITE_REFUSALS)
async def upgrade_plugin(request: Request, name: PluginName, body: PluginInstallIn, user: AdminUser) -> Plugin:
    """Move the installed version of plugin ``name`` to a newer one: the one asked for, or the newest that runs."""
    return await _install(request, user, name, body.version, upgrade=True)


@router.post("/{name}/remove", response_model=Plugin, responses=WRITE_REFUSALS)
async def remove_plugin(request: Request, name: PluginName, user: AdminUser) -> Plugin:
    """Remove the installed version of plugin ``name``; every version stays in the store."""
    async with request.app.state.engine.begin() as conn:
        row = await find(conn, name, lock=True)
        if row is None:
            raise not_found(name)
        if row.installed_version is None:
            raise HTTPException(409, f"plugin {name} is not installed")
        if from_image(await version_rows(conn, row.id)):
            raise HTTPException(409, FROM_IMAGE.format(name=name))
        await set_removed(conn, user, row)
        result = await plugin(conn, name, request.app.state.hub_version)
    log.info("plugin removed", extra={"plugin": name})
    return result


# The hub's own layer of a plugin's config


async def _installed(conn, name: str, *, lock: bool = False):
    """(Place of the instance layer, the config schema of the installed version) of plugin ``name``; 404 when it is
    not installed."""
    row = await find(conn, name, lock=lock, builtin=True)  # the hub's layer of a feature's config too
    if row is None or row.installed_version is None:
        raise HTTPException(404, f"no plugin {name} is installed on this hub: see `evo-agents hub plugin list`")
    version = next(v for v in await version_rows(conn, row.id) if v.version == row.installed_version)
    return Place(row.id, name, "instance"), version.manifest.get("config")


@router.get("/{name}/backend", response_model=BackendStatus, responses=READ_REFUSALS)
async def plugin_backend(request: Request, name: PluginName) -> BackendStatus:
    """The state of the plugin's backend as the plugin host last reported it: running, restarting, or in backoff with
    its last failure, for a hub admin to read. A plugin the host has not reported on reads as such."""
    async with request.app.state.engine.begin() as conn:
        row = await find(conn, name)
        if row is None:
            raise not_found(name)
        return await backend_status(conn, row.id, name)


@router.get("/{name}/config", response_model=PluginConfigLayer, responses=READ_REFUSALS)
async def instance_config(request: Request, name: PluginName) -> PluginConfigLayer:
    """The current version of the hub's own layer of the plugin's config."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await _installed(conn, name)
        return await read_layer(conn, place)


@router.get("/{name}/config/versions", response_model=list[PluginConfigLayer], responses=READ_REFUSALS)
async def instance_config_versions(request: Request, name: PluginName) -> list[PluginConfigLayer]:
    """Every version of the hub's own layer, the newest first."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await _installed(conn, name)
        return await layer_versions(conn, place)


@router.post("/{name}/config", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)
async def write_instance_config(
    request: Request, name: PluginName, body: PluginConfigWrite, user: AdminUser
) -> PluginConfigLayer:
    """Write the next version of the hub's own layer: the fields ``values`` sets, those ``unset`` clears."""
    async with request.app.state.engine.begin() as conn:
        place, schema = await _installed(conn, name, lock=True)
        return await write_layer(conn, user, place, schema, body, request.app.state.sealer)


@router.post("/{name}/config/rollback", response_model=PluginConfigLayer, responses=WRITE_REFUSALS)
async def rollback_instance_config(
    request: Request, name: PluginName, body: PluginConfigRollback, user: AdminUser
) -> PluginConfigLayer:
    """Copy an older version of the hub's own layer as its next version."""
    async with request.app.state.engine.begin() as conn:
        place, _ = await _installed(conn, name, lock=True)
        return await rollback_layer(conn, user, place, body)
