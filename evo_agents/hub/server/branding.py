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

import logging

from fastapi import APIRouter, Depends, Request, Response

from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_admin.models.branding import CACHED as CACHED
from evo_agents.hub.server.hub_admin.models.branding import ICO as ICO
from evo_agents.hub.server.hub_admin.models.branding import IMAGE_KIND as IMAGE_KIND
from evo_agents.hub.server.hub_admin.models.branding import IMAGE_REFUSALS as IMAGE_REFUSALS
from evo_agents.hub.server.hub_admin.models.branding import MAX_IMAGE_BYTES as MAX_IMAGE_BYTES
from evo_agents.hub.server.hub_admin.models.branding import UNAVAILABLE as UNAVAILABLE
from evo_agents.hub.server.hub_admin.models.branding import WRITE_REFUSALS as WRITE_REFUSALS
from evo_agents.hub.server.hub_admin.models.branding import Asset as Asset
from evo_agents.hub.server.hub_admin.models.branding import Branding as Branding
from evo_agents.hub.server.hub_admin.models.branding import BrandingColors as BrandingColors
from evo_agents.hub.server.hub_admin.models.branding import BrandingImage as BrandingImage
from evo_agents.hub.server.hub_admin.service import branding as service
from evo_agents.hub.server.hub_admin.service.branding import HUB as HUB
from evo_agents.hub.server.hub_admin.service.branding import view as view
from evo_agents.hub.server.hub_settings import SettingsLayer, SettingsRollback, SettingsWrite
from evo_agents.hub.server.security import AdminUser, admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/branding", tags=["branding"])
admin_router = APIRouter(
    prefix="/v1/admin/branding",
    tags=["branding"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


# What everyone reads


@router.get("", response_model=Branding)
async def show_branding(request: Request) -> Branding:
    """The hub's branding, for the web to apply; it needs no credential."""
    return await service.show(request.app.state.engine)


@router.get(
    "/{asset}",
    response_class=Response,
    responses={404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def branding_image(request: Request, asset: Asset) -> Response:
    """The logo or the favicon of the branding; 404 when it is not set. It needs no credential."""
    image = await service.held_image(request.app.state.engine, asset)
    sha256 = image["sha256"]
    data = service.cached_image(sha256)
    if data is None:
        data = await service.fetch_image(blob_store(request), asset, sha256)
    return Response(content=data, media_type=image["type"], headers=service.image_headers(sha256))


# What hub admins change


@admin_router.get("", response_model=SettingsLayer)
async def branding_layer(request: Request) -> SettingsLayer:
    """The current version of the branding, as a layer of fields."""
    return await service.layer(request.app.state.engine)


@admin_router.get("/versions", response_model=list[SettingsLayer])
async def branding_versions(request: Request) -> list[SettingsLayer]:
    """Every version of the branding, the newest first."""
    return await service.versions(request.app.state.engine)


@admin_router.post("", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_branding(request: Request, body: SettingsWrite, user: AdminUser) -> SettingsLayer:
    """Write the next version of the branding: the name and the colours ``values`` sets, the fields ``unset``
    clears (logo and favicon included); refused when a colour leaves a pair under its contrast."""
    return await service.write(request.app.state.engine, user, body)


@admin_router.post("/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_branding(request: Request, body: SettingsRollback, user: AdminUser) -> SettingsLayer:
    """Copy an older version of the branding as its next version."""
    return await service.rollback(request.app.state.engine, user, body)


@admin_router.put("/{asset}", response_model=SettingsLayer, responses=IMAGE_REFUSALS)
async def put_branding_image(request: Request, asset: Asset, body: BrandingImage, user: AdminUser) -> SettingsLayer:
    """Set the logo or the favicon of the branding, as its next version."""
    data, sha256, kind = service.image(asset, body)
    layer = await service.put_image(
        request.app.state.engine, blob_store(request), user, asset, body, data, sha256, kind
    )
    log.info("branding image set", extra={"asset": asset, "bytes": len(data)})
    return layer
