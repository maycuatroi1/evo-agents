"""What the routes of the store do (``routes.store``): list and show the plugins, publish a bundle, install, upgrade
and remove a plugin, read its backend's state and read and write the hub's own layer of its config. Market modules never import this one: installing asks
``market_licenses`` whether a licence lets it, and ``market_licenses`` imports ``service.store``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.market.service.licenses import licence_problem
from evo_agents.hub.server.plugins.models.configs import (
    Place,
    PluginConfigLayer,
    PluginConfigRollback,
    PluginConfigWrite,
)
from evo_agents.hub.server.plugins.models.host import BackendStatus
from evo_agents.hub.server.plugins.models.store import (
    FROM_IMAGE,
    NOTHING_INSTALLED,
    NOTHING_PUBLISHED,
    Plugin,
    PluginDetail,
    PluginPublished,
    PluginPublishIn,
)
from evo_agents.hub.server.plugins.repository import store as repository
from evo_agents.hub.server.plugins.service.configs import layer_versions, read_layer, rollback_layer, write_layer
from evo_agents.hub.server.plugins.service.host import backend_status
from evo_agents.hub.server.plugins.service.store import (
    check_bundle,
    check_placeable,
    check_state,
    committed,
    detail,
    fetch_bundle,
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
)
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def plugins(engine: AsyncEngine, hub_version: str) -> list[Plugin]:
    """Every plugin of the hub's store, by name (``service.store.list_plugins``), in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await list_plugins(conn, hub_version)


async def plugin_detail(engine: AsyncEngine, name: str, hub_version: str) -> PluginDetail:
    """Plugin ``name`` with every version, newest first (``service.store.detail``), in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await detail(conn, name, hub_version)


async def publish(state, store: BlobStore, user: Principal, name: str, body: PluginPublishIn) -> PluginPublished:
    """Publish the signed bundle ``body`` names as a version of plugin ``name``, after checking it whole."""
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
        row = await repository.find(conn, name)
        written = next(v for v in await repository.version_rows(conn, row.id) if v.version == checked.manifest.version)  # pyright: ignore[reportOptionalMemberAccess]
    log.info(
        "plugin published" if created else "plugin unchanged",
        extra={"plugin": name, "version": written.version, "signed_by": written.signed_by},
    )
    return PluginPublished(name=name, created=created, version=version_model(written, version))


async def install(
    state, blobs: Callable[[], BlobStore], user: Principal, name: str, wanted: str | None, *, upgrade: bool
) -> Plugin:
    """Install (or upgrade to) version ``wanted`` of plugin ``name``, the newest that runs when None; ``blobs`` gives
    the blob store once the checks of the store passed (503 when the hub has none)."""
    async with state.engine.begin() as conn:
        row = await repository.find(conn, name)
        rows = [] if row is None else await repository.version_rows(conn, row.id)
        if not rows:
            raise not_found(name)
        if from_image(rows):
            raise HTTPException(409, FROM_IMAGE.format(name=name))
        check_state(row, upgrade=upgrade, target=wanted)
        problem = await licence_problem(conn, name)
        if problem is not None:
            raise HTTPException(409, problem)
        target = pick_version(name, rows, wanted, row.installed_version if upgrade else None, state.hub_version)  # pyright: ignore[reportOptionalMemberAccess]
        check_placeable(row, target)
        trusted = await trusted_publishers(conn, state.config)
    data = await fetch_bundle(blobs(), target.sha256, target.size)
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
        row = await repository.find(conn, name, lock=True)
        check_state(row, upgrade=upgrade, target=target.version)  # as it stands now, after the checks
        await set_installed(conn, user, row, target.version, upgrade=upgrade)
        result = await plugin(conn, name, state.hub_version)
    log.info("plugin upgraded" if upgrade else "plugin installed", extra={"plugin": name, "version": target.version})
    return result


async def remove(engine: AsyncEngine, user: Principal, name: str, hub_version: str) -> Plugin:
    """Remove the installed version of plugin ``name``; every version stays in the store."""
    async with UnitOfWork(engine) as conn:
        row = await repository.find(conn, name, lock=True)
        if row is None:
            raise not_found(name)
        if row.installed_version is None:
            raise HTTPException(409, f"plugin {name} is not installed")
        if from_image(await repository.version_rows(conn, row.id)):
            raise HTTPException(409, FROM_IMAGE.format(name=name))
        await set_removed(conn, user, row)
        result = await plugin(conn, name, hub_version)
        log.info("plugin removed", extra={"plugin": name})
        return result


async def backend_of(engine: AsyncEngine, name: str) -> BackendStatus:
    async with UnitOfWork(engine) as conn:
        row = await repository.find(conn, name)
        if row is None:
            raise not_found(name)
        return await backend_status(conn, row.id, name)


async def instance_place(conn: AsyncConnection, name: str, *, lock: bool = False):
    """(Place of the instance layer, the config schema of the installed version) of plugin ``name``; 404 when it is
    not installed."""
    row = await repository.find(conn, name, lock=lock, builtin=True)  # the hub's layer of a feature's config too
    if row is None or row.installed_version is None:
        raise HTTPException(404, f"no plugin {name} is installed on this hub: see `evo-agents hub plugin list`")
    version = await repository.installed_version_row(conn, row)
    return Place(row.id, name, "instance"), version.manifest.get("config")


# The hub's own layer of a plugin's config


async def instance_config(engine: AsyncEngine, name: str) -> PluginConfigLayer:
    """The current version of the hub's own layer of plugin ``name``'s config, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        place, _ = await instance_place(conn, name)
        return await read_layer(conn, place)


async def instance_config_versions(engine: AsyncEngine, name: str) -> list[PluginConfigLayer]:
    """Every version of the hub's own layer, the newest first, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        place, _ = await instance_place(conn, name)
        return await layer_versions(conn, place)


async def write_instance_config(
    engine: AsyncEngine, user: Principal, name: str, body: PluginConfigWrite, sealer: Sealer | None
) -> PluginConfigLayer:
    """Write the next version of the hub's own layer, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        place, schema = await instance_place(conn, name, lock=True)
        return await write_layer(conn, user, place, schema, body, sealer)


async def rollback_instance_config(
    engine: AsyncEngine, user: Principal, name: str, body: PluginConfigRollback
) -> PluginConfigLayer:
    """Copy an older version of the hub's own layer as its next version, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        place, _ = await instance_place(conn, name, lock=True)
        return await rollback_layer(conn, user, place, body)
