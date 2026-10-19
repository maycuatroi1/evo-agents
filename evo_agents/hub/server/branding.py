"""The branding of the hub (criterion a11 of plan hub-agent-plugins; ``evo_agents.hub.branding``).

- GET /v1/branding, without a credential, since the sign-in page shows it: the product's name, the URLs of the logo
  and the favicon, and the colours set for each theme, null or empty for what is unset, where the web keeps its own.
  GET /v1/branding/logo and /v1/branding/favicon serve the images; their URLs carry a part of the image's SHA-256, so
  a browser may keep them, and an image that is not set is a 404.
- GET and POST /v1/admin/branding, GET .../versions and POST .../rollback: the layer, for hub admins, versioned as
  ``hub_settings`` keeps it. A write that would leave a pair of web/DESIGN.md under its contrast, in either theme, is
  refused (422) naming the pair, its colours and its ratio; a colour is kept in lower case.
- PUT /v1/admin/branding/logo and /favicon: a PNG, JPEG or WebP image (the favicon may also be an ICO) of at most
  MAX_IMAGE_BYTES, as base64 in JSON and checked by its first bytes. The hub stores it in the blob store as a blob of
  kind branding-image that it holds itself, then writes the next version with the image set. A write that unsets logo
  or favicon brings back the web's own mark or favicon.

The images served are kept in this process by their SHA-256 (at most CACHED of them), so the public routes do not
reach the blob store on every request.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, branding, tables
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server.agent_profiles import media_type
from evo_agents.hub.server.blobs import blob_store, fetch_bytes, put_bytes
from evo_agents.hub.server.errors import ErrorBody
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
from evo_agents.hub.server.security import AdminUser, admin

log = logging.getLogger(__name__)

IMAGE_KIND = "branding-image"
MAX_IMAGE_BYTES = 256 * 1024
CACHED = 8
ICO = b"\x00\x00\x01\x00"
UNAVAILABLE = "the blob store did not answer; the branding was not changed, try again shortly"
HUB = Place(BRANDING)
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422)}
IMAGE_REFUSALS = {code: {"model": ErrorBody} for code in (409, 413, 422, 503)}
Asset = Literal[branding.ASSETS]

router = APIRouter(prefix="/v1/branding", tags=["branding"])
admin_router = APIRouter(
    prefix="/v1/admin/branding",
    tags=["branding"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
_images: dict[str, bytes] = {}  # SHA-256 -> the bytes of an image served, at most CACHED


class BrandingColors(BaseModel):
    light: dict[str, str] = Field(default_factory=dict, description="token -> #rrggbb, for the light theme")
    dark: dict[str, str] = Field(default_factory=dict, description="token -> #rrggbb, for the dark theme")


class Branding(BaseModel):
    """The branding as the web applies it; what is unset is null or empty, and the web keeps its own there."""

    name: str | None = Field(description="the product's name")
    logo_url: str | None = Field(description="GET of the logo on this hub, in place of the web's mark")
    favicon_url: str | None = Field(description="GET of the favicon on this hub")
    colors: BrandingColors
    version: int = Field(description="the version of the branding; 0 while it was never set")


class BrandingImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(
        min_length=1,
        max_length=(MAX_IMAGE_BYTES + 2) // 3 * 4,
        description=f"the image, base64: PNG, JPEG or WebP (or ICO for the favicon), at most {MAX_IMAGE_BYTES} bytes",
    )
    if_version: int | None = Field(None, ge=0, description="409 unless the branding is at this version now")

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("not base64") from None
        return value


def _url(asset: str, image: dict | None) -> str | None:
    return None if not image else f"/v1/branding/{asset}?v={image['sha256'][:16]}"


def view(values: dict, version: int) -> Branding:
    colors = values.get("colors") or {}
    return Branding(
        name=values.get("name"),
        logo_url=_url("logo", values.get("logo")),
        favicon_url=_url("favicon", values.get("favicon")),
        colors=BrandingColors(light=colors.get("light") or {}, dark=colors.get("dark") or {}),
        version=version,
    )


def _check(sets: dict, after: dict) -> list[str]:
    return branding.problems(sets, after)


# What everyone reads


@router.get("", response_model=Branding)
async def show_branding(request: Request) -> Branding:
    """The hub's branding, for the web to apply; it needs no credential."""
    async with request.app.state.engine.begin() as conn:
        held = await current(conn, HUB)
    return view({}, 0) if held is None else view(held.plain, held.version)


@router.get(
    "/{asset}",
    response_class=Response,
    responses={404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def branding_image(request: Request, asset: Asset) -> Response:
    """The logo or the favicon of the branding; 404 when it is not set. It needs no credential."""
    async with request.app.state.engine.begin() as conn:
        held = await current(conn, HUB)
    image = (held.plain if held is not None else {}).get(asset)
    if not image:
        raise HTTPException(404, f"this hub's branding sets no {asset}: the web shows its own")
    sha256 = image["sha256"]
    data = _images.get(sha256)
    if data is None:
        try:
            data = await asyncio.to_thread(fetch_bytes, blob_store(request), sha256, MAX_IMAGE_BYTES)
        except BlobStoreUnavailable:
            raise HTTPException(503, "the blob store did not answer; try again shortly") from None
        if data is None:
            raise HTTPException(404, f"the {asset} of this hub's branding is not in the blob store")
        if len(_images) >= CACHED:
            _images.clear()
        _images[sha256] = data
    headers = {
        "Cache-Control": "public, max-age=86400",
        "ETag": f'"{sha256}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    }
    return Response(content=data, media_type=image["type"], headers=headers)


# What hub admins change


@admin_router.get("", response_model=SettingsLayer)
async def branding_layer(request: Request) -> SettingsLayer:
    """The current version of the branding, as a layer of fields."""
    async with request.app.state.engine.begin() as conn:
        return await read_layer(conn, HUB)


@admin_router.get("/versions", response_model=list[SettingsLayer])
async def branding_versions(request: Request) -> list[SettingsLayer]:
    """Every version of the branding, the newest first."""
    async with request.app.state.engine.begin() as conn:
        return await layer_versions(conn, HUB)


@admin_router.post("", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_branding(request: Request, body: SettingsWrite, user: AdminUser) -> SettingsLayer:
    """Write the next version of the branding: the name and the colours ``values`` sets, the fields ``unset``
    clears (logo and favicon included); refused when a colour leaves a pair under its contrast."""
    values = branding.normalized(body.values)
    async with request.app.state.engine.begin() as conn:
        return await apply(conn, user, HUB, values, body.unset, body.if_version, _check)


@admin_router.post("/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_branding(request: Request, body: SettingsRollback, user: AdminUser) -> SettingsLayer:
    """Copy an older version of the branding as its next version."""
    async with request.app.state.engine.begin() as conn:
        return await rollback_layer(conn, user, HUB, body)


def _image(asset: str, body: BrandingImage) -> tuple[bytes, str, str]:
    data = base64.b64decode(body.data, validate=True)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {MAX_IMAGE_BYTES} the {asset} may have")
    kind = media_type(data) or ("image/x-icon" if asset == "favicon" and data.startswith(ICO) else None)
    if kind is None:
        allowed = "PNG, JPEG, WebP or ICO" if asset == "favicon" else "PNG, JPEG or WebP"
        raise HTTPException(422, f"the {asset} is a {allowed} image, and this one is none; nothing changed")
    return data, hashlib.sha256(data).hexdigest(), kind


async def _hold(conn: AsyncConnection, store, user_id: int, sha256: str, data: bytes) -> None:
    """Record that the hub holds blob ``sha256``, put back first if a deletion was pending for it."""
    await blob_gc.lock_shared(conn)
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    b = tables.blobs
    await conn.execute(
        pg_insert(b)
        .values(project_id=None, sha256=sha256, size=len(data), kind=IMAGE_KIND, created_by=user_id)
        .on_conflict_do_nothing()
    )


@admin_router.put("/{asset}", response_model=SettingsLayer, responses=IMAGE_REFUSALS)
async def put_branding_image(request: Request, asset: Asset, body: BrandingImage, user: AdminUser) -> SettingsLayer:
    """Set the logo or the favicon of the branding, as its next version."""
    data, sha256, kind = _image(asset, body)
    store = blob_store(request)
    try:
        await asyncio.to_thread(put_bytes, store, sha256, data)
        async with request.app.state.engine.begin() as conn:
            await _hold(conn, store, user.user_id, sha256, data)
            image = {"sha256": sha256, "type": kind, "size": len(data)}
            layer = await apply(conn, user, HUB, {asset: image}, [], body.if_version, lambda sets, after: [])
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    log.info("branding image set", extra={"asset": asset, "bytes": len(data)})
    return layer
