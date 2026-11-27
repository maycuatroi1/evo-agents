"""The branding of the hub (``evo_agents.hub.server.branding``): the branding as the web applies it, its images kept
in this process, and the layer of fields, read, written, rolled back and given an image in a transaction each."""

from __future__ import annotations

import asyncio
import base64
import hashlib

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, branding
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable
from evo_agents.hub.server.agents.service.profiles import media_type
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blobs import fetch_bytes, put_bytes
from evo_agents.hub.server.hub_admin.models.branding import (
    CACHED,
    ICO,
    MAX_IMAGE_BYTES,
    UNAVAILABLE,
    Branding,
    BrandingColors,
    BrandingImage,
)
from evo_agents.hub.server.hub_admin.repository import branding as repository
from evo_agents.hub.server.hub_settings import (
    BRANDING,
    Place,
    SettingsLayer,
    SettingsRollback,
    SettingsWrite,
    apply,
    current,
    layer_versions,
    read_layer,
    rollback_layer,
)

HUB = Place(BRANDING)
IMAGES: dict[str, bytes] = {}  # SHA-256 -> the bytes of an image served, at most CACHED


def url(asset: str, image: dict | None) -> str | None:
    return None if not image else f"/v1/branding/{asset}?v={image['sha256'][:16]}"


def view(values: dict, version: int) -> Branding:
    colors = values.get("colors") or {}
    return Branding(
        name=values.get("name"),
        logo_url=url("logo", values.get("logo")),
        favicon_url=url("favicon", values.get("favicon")),
        colors=BrandingColors(light=colors.get("light") or {}, dark=colors.get("dark") or {}),
        version=version,
    )


def check(sets: dict, after: dict) -> list[str]:
    return branding.problems(sets, after)


async def show(engine: AsyncEngine) -> Branding:
    """The hub's branding, for the web to apply."""
    async with UnitOfWork(engine) as conn:
        held = await current(conn, HUB)
    return view({}, 0) if held is None else view(held.plain, held.version)


async def held_image(engine: AsyncEngine, asset: str) -> dict:
    """The image ``asset`` of the branding (sha256, type, size); 404 when it is not set."""
    async with UnitOfWork(engine) as conn:
        held = await current(conn, HUB)
    image = (held.plain if held is not None else {}).get(asset)
    if not image:
        raise HTTPException(404, f"this hub's branding sets no {asset}: the web shows its own")
    return image


def cached_image(sha256: str) -> bytes | None:
    """The bytes of image ``sha256`` if this process keeps them."""
    return IMAGES.get(sha256)


async def fetch_image(store: BlobStore, asset: str, sha256: str) -> bytes:
    """The bytes of image ``sha256`` from the blob store, kept in this process from now on; 503 when the store does
    not answer, 404 when it does not hold them."""
    try:
        data = await asyncio.to_thread(fetch_bytes, store, sha256, MAX_IMAGE_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    if data is None:
        raise HTTPException(404, f"the {asset} of this hub's branding is not in the blob store")
    if len(IMAGES) >= CACHED:
        IMAGES.clear()
    IMAGES[sha256] = data
    return data


def image_headers(sha256: str) -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=86400",
        "ETag": f'"{sha256}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    }


async def layer(engine: AsyncEngine) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await read_layer(conn, HUB)


async def versions(engine: AsyncEngine) -> list[SettingsLayer]:
    async with UnitOfWork(engine) as conn:
        return await layer_versions(conn, HUB)


async def write(engine: AsyncEngine, user, body: SettingsWrite) -> SettingsLayer:
    """Write the next version of the branding; refused when a colour leaves a pair under its contrast."""
    values = branding.normalized(body.values)
    async with UnitOfWork(engine) as conn:
        return await apply(conn, user, HUB, values, body.unset, body.if_version, check)


async def rollback(engine: AsyncEngine, user, body: SettingsRollback) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await rollback_layer(conn, user, HUB, body)


def image(asset: str, body: BrandingImage) -> tuple[bytes, str, str]:
    data = base64.b64decode(body.data, validate=True)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {MAX_IMAGE_BYTES} the {asset} may have")
    kind = media_type(data) or ("image/x-icon" if asset == "favicon" and data.startswith(ICO) else None)
    if kind is None:
        allowed = "PNG, JPEG, WebP or ICO" if asset == "favicon" else "PNG, JPEG or WebP"
        raise HTTPException(422, f"the {asset} is a {allowed} image, and this one is none; nothing changed")
    return data, hashlib.sha256(data).hexdigest(), kind


async def hold(conn: AsyncConnection, store: BlobStore, user_id: int, sha256: str, data: bytes) -> None:
    """Record that the hub holds blob ``sha256``, put back first if a deletion was pending for it."""
    await blob_gc.lock_shared(conn)
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    await repository.add_image_blob(conn, user_id, sha256, len(data))


async def put_image(
    engine: AsyncEngine, store: BlobStore, user, asset: str, body: BrandingImage, data: bytes, sha256: str, kind: str
) -> SettingsLayer:
    """Store image ``data`` and write the next version of the branding with it as ``asset``; 503 when the blob store
    does not answer."""
    try:
        await asyncio.to_thread(put_bytes, store, sha256, data)
        async with UnitOfWork(engine) as conn:
            await hold(conn, store, user.user_id, sha256, data)
            image = {"sha256": sha256, "type": kind, "size": len(data)}
            return await apply(conn, user, HUB, {asset: image}, [], body.if_version, lambda sets, after: [])
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
