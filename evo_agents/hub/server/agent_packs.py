"""Agent packs of plugins for agent profiles (``evo_agents.plugins.agent_packs``; ``docs/plugins.md``, section Agent
packs): GET /v1/projects/{p}/agent-packs, the packs a profile of the project may attach; the check of the packs a
create or an edit of a profile attaches (``pinned``); and at a claim, each pack the run's profile attaches at the
version it pins, with a presigned GET of its plugin's bundle for the worker (``tickets``).

A profile attaches a pack of a plugin its project enabled (installed and turned on, ``plugin_calls.enabled_query``),
at a version of that plugin the hub holds as a bundle: one that runs on this release (``compat.problem``) and whose
manifest declares the pack. A core plugin that comes with the hub's image (builder's @builder) holds its packs in its
package: the version the image ships now backs them, its bundle built from the package at a claim
(``plugins.core.image_bundle``) and put in the blob store when it is not there yet; a version the image no longer
ships backs none. Each pack once, at most ``profiles.MAX_PACKS``; a pin left without a version takes the version
installed now.
The pin holds when the plugin moves to another version: only an edit of the profile moves it, as a new version of
the profile. The default profile attaches none, so a run that names no agent runs as before.

At a claim, a run whose pack's plugin is no longer enabled in the run's project, whose pinned version the hub no
longer holds, or that the hub cannot hand out for want of a blob store, fails as the reaper, saying why (``claims``).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, tables
from evo_agents.hub.blobs import BLOB_PREFIX, MAX_GET_TTL, BlobStoreUnavailable
from evo_agents.hub.plugin_calls import core_registry, enabled_in, enabled_plugins
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import RunPack
from evo_agents.hub.server.security import CurrentUser
from evo_agents.plugins import compat, core
from evo_agents.plugins.agent_packs import pack_name
from evo_agents.plugins.bundle import Bundle, BundleError
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.runtimes import CONTRIBUTION

POINT = "agent_packs"
NOTHING = "nothing changed"
IMAGE_KEY = "image:"  # plugin_versions.r2_key of a core plugin of the hub's image: image:<entry point>

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


class AgentPackPin(BaseModel):
    """A pack a profile attaches: pack ``name`` of plugin ``plugin`` at version ``version`` of the plugin."""

    model_config = ConfigDict(extra="forbid")

    plugin: str = Field(pattern=PLUGIN_ID, description="the plugin's id")
    name: str = Field(pattern=f"^{CONTRIBUTION}$", description="the pack, as the plugin's manifest names it")
    version: str | None = Field(
        None,
        pattern=f"^{compat.PLUGIN_VERSION.pattern}$",
        description="a version of the plugin the hub holds; left out: the one installed now, which the profile pins",
    )

    @property
    def pack(self) -> str:
        return pack_name(self.plugin, self.name)


Pins = Annotated[list[AgentPackPin], Field(max_length=profiles.MAX_PACKS)]


class PinnedPack(BaseModel):
    """A pack a profile attaches, at the version of its plugin it pins."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    version: str


def pinned_view(packs: list | None) -> list[PinnedPack]:
    """The packs of a profile (as it stores them) as its view shows them."""
    return [PinnedPack(pack=pack_name(item["plugin"], item["name"]), **item) for item in packs or ()]


class AgentPack(BaseModel):
    """A pack a profile of the project may attach: of a plugin the project enabled, at one of ``versions``."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    title: str | None
    description: str | None
    installed_version: str = Field(description="the plugin's version installed now, which a pin without one takes")
    versions: list[str] = Field(
        description="the versions of the plugin the hub holds that declare the pack and run on this release, the "
        "newest first"
    )


def _declared(manifest: dict, name: str) -> dict | None:
    """The contribution of pack ``name`` in ``manifest`` (as stored), or None."""
    return next((item for item in (manifest.get("contributes") or {}).get(POINT, ()) if item["name"] == name), None)


def _unfit(row, name: str, hub_version: str) -> str | None:
    """Why version ``row`` (of plugin_versions) cannot back pack ``name``; None when it can."""
    if not _bundled(row) and image_plugin(row) is None:
        return "comes with the hub's image, with no bundle a worker could build the pack from"
    manifest = row.manifest
    if _declared(manifest, name) is None:
        return f"declares no agent pack {name}"
    found = compat.problem(manifest["engines"], manifest["plugin_api"], hub_version, manifest.get("contributes"))
    return None if found is None else f"does not run on this hub: {found}"


def _bundled(row) -> bool:
    """Whether version ``row`` of a plugin is a bundle in the blob store; a core plugin that comes with the hub's image
    (``plugin_store.IMAGE``) is none."""
    return row.r2_key == BLOB_PREFIX + row.sha256


def image_plugin(row) -> core.CorePlugin | None:
    """The core plugin of this process that version ``row`` of a plugin of the hub's image stands for, when the
    image ships that very version; None otherwise."""
    if not row.r2_key.startswith(IMAGE_KEY):
        return None
    value = row.r2_key[len(IMAGE_KEY) :]
    found = (plugin for plugin in core_registry().plugins if plugin.value == value)
    return next((plugin for plugin in found if plugin.manifest.version == row.version), None)


def _versions(plugin_id: int):
    v = tables.plugin_versions
    return select(v.c.version, v.c.manifest, v.c.r2_key, v.c.sha256, v.c.size).where(v.c.plugin_id == plugin_id)


async def _version(conn: AsyncConnection, plugin_id: int, version: str):
    query = _versions(plugin_id).where(tables.plugin_versions.c.version == version)
    return (await conn.execute(query)).one_or_none()


async def _pin(conn: AsyncConnection, project_id: int, project: str, pin: AgentPackPin, hub_version: str) -> dict:
    enabled = await enabled_in(conn, pin.plugin, project_id)
    if enabled is None:
        raise HTTPException(
            422,
            f"agent pack {pin.pack}: plugin {pin.plugin} is not enabled in project {project}, or it is turned off; "
            f"see GET /v1/projects/{project}/agent-packs; {NOTHING}",
        )
    version = pin.version or enabled.installed_version
    row = await _version(conn, enabled.id, version)
    why = f"the hub holds no version {version} of plugin {pin.plugin}" if row is None else None
    if row is not None and (unfit := _unfit(row, pin.name, hub_version)) is not None:
        why = f"version {version} of plugin {pin.plugin} {unfit}"
    if why is not None:
        raise HTTPException(422, f"agent pack {pin.pack}: {why}; {NOTHING}")
    return {"plugin": pin.plugin, "name": pin.name, "version": version}


async def pinned(
    conn: AsyncConnection, project_id: int, project: str, pins: Iterable[AgentPackPin] | None, hub_version: str
) -> list[dict] | None:
    """The packs ``pins`` attach, as a profile of project ``project`` stores them (``{"plugin", "name", "version"}``),
    in their order (None for None, an edit that leaves them); 422 for a pack attached twice, or one ``_pin``
    refuses."""
    if pins is None:
        return None
    found, seen = [], set()
    for pin in pins:
        if pin.pack in seen:
            raise HTTPException(422, f"agent pack {pin.pack} is attached twice: attach each pack once; {NOTHING}")
        seen.add(pin.pack)
        found.append(await _pin(conn, project_id, project, pin, hub_version))
    return found


@router.get(
    "/{project}/agent-packs",
    response_model=list[AgentPack],
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def list_agent_packs(request: Request, project: ProjectName, user: CurrentUser) -> list[AgentPack]:
    """The agent packs a profile of the project may attach, by plugin, then in its manifest's order."""
    hub_version = request.app.state.hub_version
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the agent packs of project {project} needs a grant on it")
        found = []
        for plugin in await enabled_plugins(conn, access.project_id):
            items = (plugin.manifest.get("contributes") or {}).get(POINT, ())
            held = list(await conn.execute(_versions(plugin.id))) if items else []
            for item in items:
                usable = [row.version for row in held if _unfit(row, item["name"], hub_version) is None]
                found.append(
                    AgentPack(
                        pack=pack_name(plugin.name, item["name"]),
                        plugin=plugin.name,
                        name=item["name"],
                        title=item.get("title"),
                        description=item.get("description"),
                        installed_version=plugin.installed_version,
                        versions=sorted(usable, key=compat.plugin_version_key, reverse=True),
                    )
                )
        return found


# Claiming


async def _held(conn: AsyncConnection, project_id: int, project: str, pack: dict) -> tuple[object, str | None]:
    """The version row pack ``pack`` (as a profile stores it) pins, and None; or None and why the run cannot have it."""
    plugin, name, version = pack["plugin"], pack["name"], pack["version"]
    enabled = await enabled_in(conn, plugin, project_id)
    if enabled is None:
        return None, (
            f"plugin {plugin} of agent pack {pack_name(plugin, name)} is no longer enabled in project {project}: it was "
            "disabled there, turned off or removed since the run was dispatched; enable it and dispatch the run again"
        )
    row = await _version(conn, enabled.id, version)
    usable = row is not None and (_bundled(row) or image_plugin(row) is not None)
    if not usable or _declared(row.manifest, name) is None:
        return None, (
            f"the hub no longer holds version {version} of plugin {plugin} with agent pack {name}, which the run's "
            "agent profile pins: attach a version it holds to the profile, then dispatch the run again"
        )
    return row, None


def _put_image(blobs, plugin: core.CorePlugin) -> Bundle:
    """The bundle of core plugin ``plugin``'s packs, in the blob store ``blobs`` once this returns."""
    built = core.image_bundle(plugin)
    if blobs.has_blob(built.sha256):
        return built
    handle, path = tempfile.mkstemp(prefix="evo-image-bundle-", suffix=".tar.gz")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(built.data)
        blobs.put_file(built.sha256, Path(path))
    finally:
        os.unlink(path)
    return built


async def _blob_of(blobs, row) -> tuple[str, int]:
    """(SHA-256, size) of the bundle that backs version ``row``: its own, or the one built from the image's package."""
    if _bundled(row):
        return row.sha256, row.size
    built = await asyncio.to_thread(_put_image, blobs, image_plugin(row))
    return built.sha256, len(built.data)


async def tickets(
    conn: AsyncConnection, blobs, project_id: int, project: str, packs: list[dict]
) -> tuple[list[RunPack], str | None]:
    """The packs ``packs`` (a profile version's) at the versions they pin, each with a presigned GET of its plugin's
    bundle in the blob store ``blobs`` that works MAX_GET_TTL; or no pack and why the run cannot have them."""
    held = []
    for pack in packs:
        row, why = await _held(conn, project_id, project, pack)
        if why is not None:
            return [], why
        held.append((pack, row))
    if blobs is None:
        return [], (
            "the hub has no blob store, so it cannot hand the worker the bundles of the agent packs the run's profile "
            "attaches: configure the blob store of the hub, then dispatch the run again"
        )
    found = []
    for pack, row in held:
        try:
            sha256, size = await _blob_of(blobs, row)
        except (BundleError, BlobStoreUnavailable) as exc:
            return [], (
                f"the hub cannot hand out the packs of core plugin {pack['plugin']} {pack['version']} of its image: "
                f"{exc}; dispatch the run again once the hub's image or blob store is fixed"
            )
        expires_at = datetime.now(UTC) + MAX_GET_TTL  # taken before signing, so never later than the URL
        filename = f"{pack['plugin']}-{pack['version']}.tar.gz"
        url = await asyncio.to_thread(blobs.presign_get, sha256, MAX_GET_TTL, filename=filename)
        found.append(
            RunPack(
                pack=pack_name(pack["plugin"], pack["name"]),
                plugin=pack["plugin"],
                name=pack["name"],
                version=pack["version"],
                sha256=sha256,
                size=size,
                url=url,
                expires_at=expires_at,
            )
        )
    return found, None
